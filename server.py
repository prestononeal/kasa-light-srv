import argparse
import asyncio
import ipaddress
import json
import logging
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from astral import Observer
from astral.sun import sunrise, sunset
from kasa import AuthenticationError, Discover
from kasa.iot import IotPlug, IotStrip

ROTATION_INTERVAL = 300
OUTLETS = tuple(str(port) for port in range(1, 7))
LOG = logging.getLogger(__name__)


class Schedule:
    def __init__(self, config):
        try:
            latitude = float(config.get("latitude"))
            longitude = float(config.get("longitude"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Set latitude and longitude to numeric coordinates for the strip's location"
            ) from exc
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Set valid latitude and longitude in config.json")
        self.observer = Observer(latitude, longitude)
        self.zone = ZoneInfo(config["timezone"])

    def current(self, now):
        day = now.astimezone(self.zone).date()
        events = []
        for offset in range(-2, 3):
            date = day + timedelta(days=offset)
            for name, calculate in (("sunrise", sunrise), ("sunset", sunset)):
                try:
                    time = calculate(self.observer, date, self.zone)
                except ValueError:
                    continue
                events.append((time.astimezone(timezone.utc), name))
        previous = max((event for event in events if event[0] <= now), default=None)
        upcoming = min((event for event in events if event[0] > now), default=None)
        night = previous and previous[1] == "sunset" and upcoming and upcoming[1] == "sunrise"
        return (
            previous[0].isoformat() if night else None,
            upcoming[1] if upcoming else None,
            upcoming[0].astimezone(self.zone).isoformat() if upcoming else None,
        )


def discovery_options():
    return {
        "timeout": 3,
        "discovery_timeout": 3,
        "username": os.environ.get("KASA_USERNAME"),
        "password": os.environ.get("KASA_PASSWORD"),
    }


async def close_device(device):
    try:
        await device.disconnect()
    except Exception as exc:
        LOG.debug("Error closing device connection: %s", exc)


def strip_device(device):
    # KLAP discovery can identify an HS300 as a plain plug.
    if isinstance(device, IotPlug) and device.model.startswith("HS300"):
        return IotStrip(device.host, protocol=device.protocol)
    return device


class PowerStrip:
    def __init__(self, address):
        self.host = address if isinstance(address, str) else None
        self.alias = address.get("alias") if isinstance(address, dict) else None
        if isinstance(address, str):
            if not self.host.strip() or self.host == "192.0.2.10":
                raise ValueError("Set each HS300's address")
        elif not (isinstance(address, dict) and address.keys() == {"alias"}
                  and isinstance(self.alias, str) and self.alias.strip()):
            raise ValueError('Use a host address or {"alias": "Kasa strip name"} for each strip')
        self.device = None
        self.actual = {}
        self.error = "Connecting to the HS300…"

    async def disconnect(self):
        device, self.device = self.device, None
        if device is not None:
            await close_device(device)

    async def connection_failed(self, exc):
        LOG.warning("HS300 %s update failed: %s", self.alias or self.host, exc)
        if isinstance(exc, AuthenticationError):
            self.error = "HS300 authentication failed. Check KASA_USERNAME and KASA_PASSWORD."
        else:
            self.error = "HS300 unavailable. Retrying automatically."
        self.actual = {}
        await self.disconnect()

    async def prepare(self):
        if self.alias is not None and self.device is None:
            return
        try:
            if self.device is None:
                self.device = await Discover.discover_single(self.host, **discovery_options())
            if self.device is None:
                raise RuntimeError("No device answered discovery")
            self.device = strip_device(self.device)
            await self.device.update(update_children=False)
            if not self.device.model.startswith("HS300") or len(self.device.children) != 6:
                raise RuntimeError("Expected an HS300 with six individually controllable outlets")
            if self.alias is not None and self.device.alias != self.alias:
                raise RuntimeError("The device no longer has the configured Kasa alias")
        except Exception as exc:
            await self.connection_failed(exc)

    async def apply(self, on_ports):
        try:
            outlets = dict(zip(OUTLETS, self.device.children))
            observed = {port: outlet.is_on for port, outlet in outlets.items()}
            if self.error:
                LOG.info("Connected to HS300 %r at %s", self.alias or self.host, self.device.host)
            self.log_outlet_changes("Observed", self.actual, observed)
            changed = False
            for desired in (False, True):
                for port, outlet in outlets.items():
                    if (port in on_ports) == desired and outlet.is_on != desired:
                        await (outlet.turn_on() if desired else outlet.turn_off())
                        changed = True
            if changed:
                await self.device.update(update_children=False)
            self.actual = {port: outlet.is_on for port, outlet in outlets.items()}
            self.log_outlet_changes("Changed", observed, self.actual)
            self.error = None
        except Exception as exc:
            await self.connection_failed(exc)

    def log_outlet_changes(self, action, before, after):
        for port, on in after.items():
            if port in before and before[port] != on:
                LOG.info("%s HS300 %r outlet %s: %s", action, self.alias or self.host, port,
                         "on" if on else "off")


async def discover_aliases(strips):
    pending = {strip.alias: strip for strip in strips if strip.alias is not None and strip.device is None}
    if not pending:
        return

    try:
        devices = await Discover.discover(**discovery_options())
    except Exception:
        LOG.exception("Kasa alias discovery failed")
        for strip in pending.values():
            strip.error = "Kasa discovery failed. Retrying automatically."
        return

    async def identify(host, device):
        try:
            if not device.model.startswith("HS300"):
                return None
            device = devices[host] = strip_device(device)
            await device.update(update_children=False)
            return device.alias
        except Exception as exc:
            LOG.debug("Could not identify a discovered Kasa device: %s", exc)
            return None

    retained = set()
    try:
        aliases = await asyncio.gather(*(identify(host, device) for host, device in devices.items()))
        matches = {alias: [] for alias in pending}
        for host, alias in zip(devices, aliases):
            if alias in matches:
                matches[alias].append(host)
        for alias, strip in pending.items():
            hosts = matches[alias]
            if len(hosts) == 1:
                strip.device = devices[hosts[0]]
                retained.add(hosts[0])
            elif hosts:
                strip.error = f'More than one HS300 is named "{alias}". Give each strip a unique Kasa name.'
            else:
                strip.error = f'Could not find an HS300 named "{alias}". Check its Kasa name and credentials.'
    finally:
        await asyncio.gather(*(close_device(device) for host, device in devices.items() if host not in retained))


def configured_lights(config):
    lights = config["lights"]
    if not isinstance(lights, dict):
        raise ValueError("lights must map light IDs to settings")

    # Legacy single-strip configs use outlet numbers as their light IDs.
    legacy = "strips" not in config
    addresses = {"main": config["strip_host"]} if legacy else config["strips"]
    if not isinstance(addresses, dict) or not addresses:
        raise ValueError("strips must map strip IDs to HS300 addresses or aliases")
    strips = {name: PowerStrip(address) for name, address in addresses.items()}
    if len({(strip.host, strip.alias) for strip in strips.values()}) != len(strips):
        raise ValueError("Each strip needs a unique address or Kasa alias")

    mapped = {}
    used_ports = set()
    for light_id, light in lights.items():
        if (not re.fullmatch(r"[a-zA-Z0-9_-]+", light_id) or not isinstance(light, dict)
                or not isinstance(light.get("name"), str) or not light["name"].strip()
                or type(light.get("on_start")) is not bool):
            raise ValueError("Each light needs a valid ID, a name, and a boolean on_start")
        strip = "main" if legacy else light.get("strip")
        port = light_id if legacy else str(light.get("port"))
        if not isinstance(strip, str) or strip not in strips or port not in OUTLETS:
            raise ValueError(f"Light {light_id} needs a configured strip and outlet 1 through 6")
        if (strip, port) in used_ports:
            raise ValueError(f"Strip {strip} outlet {port} has more than one light")
        used_ports.add((strip, port))
        window = light.get("window", "")
        if not isinstance(window, str):
            raise ValueError(f"Light {light_id} needs a text window label")
        mapped[light_id] = {
            "id": light_id, "name": light["name"], "window": window.strip(),
            "on_start": light["on_start"], "strip": strip, "port": port,
        }
    return strips, mapped


def utc_now():
    return datetime.now(timezone.utc)


class Controller:
    def __init__(self, config, state_path):
        self.strips, self.lights = configured_lights(config)
        self.schedule = Schedule(config)
        self.state_path = state_path
        self.overrides = json.loads(state_path.read_text()) if state_path.exists() else {}
        self.lock = asyncio.Lock()
        self.last_phase = None

    def save(self, state):
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(self.state_path)
        self.overrides = state

    def night_state(self, night):
        rotation_enabled = self.overrides.get("rotation_enabled", True)
        if night and self.overrides.get("night") == night:
            return {**self.overrides, "rotation_enabled": rotation_enabled}
        return {"night": night, "rotation_enabled": rotation_enabled}

    def desired(self, night, state):
        if not night:
            return {}
        if state.get("rotating"):
            return {state["selected"]: True}
        overrides = state.get("lights", {})
        return {
            light_id: overrides.get(light_id, light["on_start"])
            for light_id, light in self.lights.items()
        }

    def rotation_deadline(self, night, state):
        if night and state["rotation_enabled"] and self.lights:
            return state.get("rotation_at") or (
                datetime.fromisoformat(night).timestamp() + ROTATION_INTERVAL
            )
        return None

    async def disconnect(self):
        await asyncio.gather(*(strip.disconnect() for strip in self.strips.values()))

    async def reconcile(self):
        now = utc_now()
        night, _, _ = self.schedule.current(now)
        phase = "night" if night else "daytime"
        if phase != self.last_phase:
            LOG.info("Lighting schedule entered %s", phase)
            self.last_phase = phase
        state = self.night_state(night)
        deadline = self.rotation_deadline(night, state)
        if deadline is not None and now.timestamp() >= deadline:
            previous = state.get("selected")
            choices = [light_id for light_id in self.lights if light_id != previous]
            selected = random.choice(choices or list(self.lights))
            state = {
                "night": night, "rotation_enabled": True, "rotating": True,
                "selected": selected, "rotation_at": now.timestamp() + ROTATION_INTERVAL,
            }
            self.save(state)
            light = self.lights[selected]
            LOG.info("Rotation selected %r (%s, outlet %s)", light["name"], light["strip"], light["port"])

        by_strip = {name: set() for name in self.strips}
        for light_id, on in self.desired(night, state).items():
            if on and light_id in self.lights:
                light = self.lights[light_id]
                by_strip[light["strip"]].add(light["port"])
        await discover_aliases(self.strips.values())
        await asyncio.gather(*(strip.prepare() for strip in self.strips.values()))
        connected = {}
        for name, strip in self.strips.items():
            if strip.device is not None:
                connected.setdefault(strip.device.host, []).append(name)
        ready = set()
        for names in connected.values():
            if len(names) == 1:
                ready.add(names[0])
            else:
                for name in names:
                    strip = self.strips[name]
                    strip.actual = {}
                    strip.error = "Multiple config entries refer to this HS300."

        # Turn off outlets on other strips before lighting a new rotation pick.
        active = self.lights.get(state.get("selected"), {}) if state.get("rotating") else {}
        active_strip = active.get("strip")
        await asyncio.gather(*(
            self.strips[name].apply(by_strip[name])
            for name in ready if name != active_strip
        ))
        if active_strip in ready:
            await self.strips[active_strip].apply(by_strip[active_strip])

    def status(self):
        now = utc_now()
        night, event, time = self.schedule.current(now)
        state = self.night_state(night)
        deadline = self.rotation_deadline(night, state)
        selected = self.lights.get(state.get("selected"), {})
        errors = {name: strip.error for name, strip in self.strips.items() if strip.error}
        return {
            "night": bool(night), "timezone": self.schedule.zone.key, "next_event": event,
            "next_time": time, "error": " · ".join(f"{name}: {error}" for name, error in errors.items()),
            "rotation": {
                "enabled": state["rotation_enabled"],
                "next_at": deadline,
                "server_now": now.timestamp(),
                "manual": bool(state.get("lights")),
                "active": selected.get("name") if state.get("rotating") else None,
                "window": selected.get("window") if state.get("rotating") else None,
            },
            "lights": [
                {**light, "on": self.strips[light["strip"]].actual.get(light["port"])}
                for light in self.lights.values()
            ],
        }

    async def toggle(self, request):
        light_id = request.match_info["port"]
        light = self.lights.get(light_id)
        if light is None:
            raise web.HTTPNotFound(text="That light is not configured.")
        on = await boolean_field(request, "on")
        async with self.lock:
            now = utc_now()
            night, _, _ = self.schedule.current(now)
            if night is None:
                raise web.HTTPConflict(text="Controls are available from sunset until sunrise.")
            strip = self.strips[light["strip"]]
            if strip.error:
                raise web.HTTPServiceUnavailable(text=strip.error)
            state = dict(self.night_state(night))
            lights = self.desired(night, state) if state.get("rotating") else dict(state.get("lights", {}))
            # Keep other outlets at their current settings when manual control starts.
            if state.get("rotating"):
                lights = {key: lights.get(key, False) for key in self.lights}
            lights[light_id] = on
            state.update(lights=lights, rotating=False, rotation_at=now.timestamp() + ROTATION_INTERVAL)
            self.save(state)
            LOG.info("Manual request for %r (%s, outlet %s): %s", light["name"], light["strip"],
                     light["port"], "on" if on else "off")
            await self.reconcile()
            return web.json_response(self.status(), status=202 if strip.error else 200)

    async def set_rotation(self, request):
        enabled = await boolean_field(request, "enabled")
        if enabled and not self.lights:
            raise web.HTTPConflict(text="Configure at least one light to enable rotation.")
        async with self.lock:
            now = utc_now()
            night, _, _ = self.schedule.current(now)
            state = dict(self.night_state(night))
            if enabled != state["rotation_enabled"]:
                state.update(rotation_enabled=enabled, rotating=False)
                state["rotation_at"] = now.timestamp() + ROTATION_INTERVAL if night and enabled else None
                self.save(state)
                LOG.info("Rotation %s from dashboard", "enabled" if enabled else "disabled")
                await self.reconcile()
            return web.json_response(self.status())

    async def run(self):
        while True:
            async with self.lock:
                try:
                    await self.reconcile()
                except OSError:
                    LOG.exception("Could not save rotation state")
            await asyncio.sleep(5)


async def boolean_field(request, field):
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Expected a JSON object.")
    if not isinstance(body, dict) or type(body.get(field)) is not bool:
        raise web.HTTPBadRequest(text=f"Expected a boolean {field} field.")
    return body[field]


@web.middleware
async def check_host(request, handler):
    # LAN IPs and localhost work without accepting arbitrary DNS names.
    host = request.host.rsplit(":", 1)[0] if ":" in request.host else request.host
    if host != "localhost":
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise web.HTTPBadRequest(text="Open the dashboard using the server's IPv4 address.")
    return await handler(request)


def make_app(controller):
    app = web.Application(middlewares=[check_host], client_max_size=4096)

    async def worker(_app):
        task = asyncio.create_task(controller.run())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await controller.disconnect()

    async def index(_request):
        return web.FileResponse(Path(__file__).with_name("index.html"))

    async def status(_request):
        return web.json_response(controller.status(), headers={"Cache-Control": "no-store"})

    app.cleanup_ctx.append(worker)
    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/lights/{port}", controller.toggle)
    app.router.add_post("/api/rotation", controller.set_rotation)
    return app


def main():
    parser = argparse.ArgumentParser(description="Serve the HS300 Christmas lights dashboard")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--watch", action="store_true", help="Restart on development file changes")
    args = parser.parse_args()
    if args.watch:
        from dev_watch import run

        source = Path(__file__).resolve()
        run(
            [sys.executable, "-u", str(source), "--config", str(args.config), "--port", str(args.port)],
            [source, source.with_name("index.html"), args.config],
        )
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S %z")
    try:
        config = json.loads(args.config.read_text())
        controller = Controller(config, args.config.with_suffix(".overrides.json"))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(f"Could not load {args.config}: {exc}")
    web.run_app(make_app(controller), host="0.0.0.0", port=args.port, access_log=None)


if __name__ == "__main__":
    main()

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


class PowerStrip:
    def __init__(self, host):
        if not isinstance(host, str) or not host.strip() or host == "192.0.2.10":
            raise ValueError("Set each HS300's IP address")
        self.host = host
        self.device = None
        self.actual = {}
        self.error = "Connecting to the HS300…"

    async def disconnect(self):
        device, self.device = self.device, None
        if device is not None:
            try:
                await device.disconnect()
            except Exception as exc:
                LOG.debug("Error closing the HS300 connection: %s", exc)

    async def reconcile(self, on_ports):
        try:
            if self.device is None:
                self.device = await Discover.discover_single(
                    self.host, timeout=3, discovery_timeout=3,
                    username=os.environ.get("KASA_USERNAME"),
                    password=os.environ.get("KASA_PASSWORD"),
                )
            if self.device is None:
                raise RuntimeError("No device answered discovery")
            # KLAP discovery can identify this strip as a plain IOT plug.
            if isinstance(self.device, IotPlug) and self.device.model.startswith("HS300"):
                self.device = IotStrip(self.host, protocol=self.device.protocol)
            await self.device.update(update_children=False)
            if not self.device.model.startswith("HS300") or len(self.device.children) != 6:
                raise RuntimeError("Expected an HS300 with six individually controllable outlets")
            outlets = dict(zip(OUTLETS, self.device.children))
            changed = False
            for desired in (False, True):
                for port, outlet in outlets.items():
                    if (port in on_ports) == desired and outlet.is_on != desired:
                        await (outlet.turn_on() if desired else outlet.turn_off())
                        changed = True
            if changed:
                await self.device.update(update_children=False)
            self.actual = {port: outlet.is_on for port, outlet in outlets.items()}
            self.error = None
        except Exception as exc:
            LOG.warning("HS300 at %s update failed: %s", self.host, exc)
            if isinstance(exc, AuthenticationError):
                self.error = "HS300 authentication failed. Check KASA_USERNAME and KASA_PASSWORD."
            else:
                self.error = "HS300 unavailable. Retrying automatically."
            self.actual = {}
            await self.disconnect()


def configured_lights(config):
    lights = config["lights"]
    if not isinstance(lights, dict):
        raise ValueError("lights must map light IDs to settings")

    # Legacy single-strip configs use outlet numbers as their light IDs.
    legacy = "strips" not in config
    hosts = {"main": config["strip_host"]} if legacy else config["strips"]
    if not isinstance(hosts, dict) or not hosts:
        raise ValueError("strips must map strip names to HS300 IP addresses")
    strips = {name: PowerStrip(host) for name, host in hosts.items()}
    if len({strip.host for strip in strips.values()}) != len(strips):
        raise ValueError("Each strip needs a unique address")

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

        by_strip = {name: set() for name in self.strips}
        for light_id, on in self.desired(night, state).items():
            if on and light_id in self.lights:
                light = self.lights[light_id]
                by_strip[light["strip"]].add(light["port"])
        # Turn off outlets on other strips before lighting a new rotation pick.
        active = self.lights.get(state.get("selected"), {}) if state.get("rotating") else {}
        active_strip = active.get("strip")
        await asyncio.gather(*(
            strip.reconcile(by_strip[name])
            for name, strip in self.strips.items() if name != active_strip
        ))
        if active_strip:
            await self.strips[active_strip].reconcile(by_strip[active_strip])

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
    logging.basicConfig(level=logging.INFO)
    try:
        config = json.loads(args.config.read_text())
        controller = Controller(config, args.config.with_suffix(".overrides.json"))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(f"Could not load {args.config}: {exc}")
    web.run_app(make_app(controller), host="0.0.0.0", port=args.port, access_log=None)


if __name__ == "__main__":
    main()

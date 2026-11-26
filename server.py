import argparse
import asyncio
import ipaddress
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from astral import Observer
from astral.sun import sunrise, sunset
from kasa import AuthenticationError, Discover
from kasa.iot import IotPlug, IotStrip

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


class Controller:
    def __init__(self, config, state_path):
        self.host = config["strip_host"]
        if not isinstance(self.host, str) or not self.host.strip() or self.host == "192.0.2.10":
            raise ValueError("Set strip_host to your HS300's IP address")
        self.lights = config["lights"]
        if not isinstance(self.lights, dict) or not self.lights.keys() <= set(OUTLETS):
            raise ValueError("lights must map outlet numbers 1 through 6 to light settings")
        for light in self.lights.values():
            if (not isinstance(light, dict) or not isinstance(light.get("name"), str)
                    or not light["name"].strip() or type(light.get("on_start")) is not bool):
                raise ValueError("Each light needs a name and a boolean on_start")
        self.schedule = Schedule(config)
        self.state_path = state_path
        self.overrides = json.loads(state_path.read_text()) if state_path.exists() else {}
        self.lock = asyncio.Lock()
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

    async def reconcile(self):
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
            # Reuse its negotiated protocol with the outlet-aware device class.
            if isinstance(self.device, IotPlug) and self.device.model.startswith("HS300"):
                self.device = IotStrip(self.host, protocol=self.device.protocol)
            await self.device.update(update_children=False)
            if not self.device.model.startswith("HS300") or len(self.device.children) != 6:
                raise RuntimeError("Expected an HS300 with six individually controllable outlets")
            changed = False
            for port, outlet in zip(OUTLETS, self.device.children):
                night, _, _ = self.schedule.current(datetime.now(timezone.utc))
                defaults = self.lights.get(port, {}).get("on_start", False)
                overrides = self.overrides.get("lights", {}) if night == self.overrides.get("night") else {}
                desired = bool(night) and overrides.get(port, defaults)
                if outlet.is_on != desired:
                    await (outlet.turn_on() if desired else outlet.turn_off())
                    changed = True
            if changed:
                await self.device.update(update_children=False)
            self.actual = {port: outlet.is_on for port, outlet in zip(OUTLETS, self.device.children)}
            self.error = None
        except Exception as exc:
            LOG.warning("HS300 update failed: %s", exc)
            if isinstance(exc, AuthenticationError):
                self.error = "HS300 authentication failed. Check KASA_USERNAME and KASA_PASSWORD."
            else:
                self.error = "HS300 unavailable. Retrying automatically."
            self.actual = {}
            await self.disconnect()

    def status(self):
        night, event, time = self.schedule.current(datetime.now(timezone.utc))
        return {
            "night": bool(night), "timezone": self.schedule.zone.key, "next_event": event, "next_time": time, "error": self.error,
            "lights": [
                {"port": port, **self.lights[port], "on": self.actual.get(port)}
                for port in OUTLETS if port in self.lights
            ],
        }

    async def toggle(self, request):
        port = request.match_info["port"]
        if port not in self.lights:
            raise web.HTTPNotFound(text="That outlet has no light mapped.")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="Expected a JSON object.")
        if not isinstance(body, dict) or type(body.get("on")) is not bool:
            raise web.HTTPBadRequest(text="Expected a boolean on field.")
        async with self.lock:
            night, _, _ = self.schedule.current(datetime.now(timezone.utc))
            if night is None:
                raise web.HTTPConflict(text="Controls are available from sunset until sunrise.")
            if self.error:
                raise web.HTTPServiceUnavailable(text=self.error)
            lights = dict(self.overrides.get("lights", {})) if self.overrides.get("night") == night else {}
            lights[port] = body["on"]
            overrides = {"night": night, "lights": lights}
            temporary = self.state_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(overrides))
            temporary.replace(self.state_path)
            self.overrides = overrides
            await self.reconcile()
            if self.error:
                return web.json_response(self.status(), status=202)
            return web.json_response(self.status())

    async def run(self):
        while True:
            async with self.lock:
                await self.reconcile()
            await asyncio.sleep(5)


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

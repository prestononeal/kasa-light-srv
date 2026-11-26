import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from astral.sun import sunrise, sunset

from server import Controller, Schedule, make_app

CONFIG = {
    "strip_host": "127.0.0.1",
    "latitude": 41.8781,
    "longitude": -87.6298,
    "timezone": "America/Chicago",
    "lights": {
        "1": {"name": "Roofline", "on_start": True},
        "2": {"name": "Porch", "on_start": False},
    },
}


class Outlet:
    def __init__(self, on):
        self.is_on = on

    async def turn_on(self):
        self.is_on = True

    async def turn_off(self):
        self.is_on = False


class Strip:
    model = "HS300"

    def __init__(self):
        self.children = [Outlet(True) for _ in range(6)]

    async def update(self, *, update_children):
        pass

    async def disconnect(self):
        pass


class ServerTests(unittest.IsolatedAsyncioTestCase):
    def test_solar_boundaries_and_midnight(self):
        schedule = Schedule(CONFIG)
        date = datetime(2026, 11, 25).date()
        dusk = sunset(schedule.observer, date, schedule.zone)
        dawn = sunrise(schedule.observer, date + timedelta(days=1), schedule.zone)
        self.assertIsNone(schedule.current(dusk - timedelta(seconds=1))[0])
        night, event, time = schedule.current(dusk)
        self.assertEqual(night, dusk.astimezone(timezone.utc).isoformat())
        self.assertEqual((event, time), ("sunrise", dawn.isoformat()))
        self.assertEqual(schedule.current(dawn - timedelta(seconds=1))[0], night)
        self.assertIsNone(schedule.current(dawn)[0])

    async def test_manual_controls_restart_and_sunrise(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            controller = Controller(CONFIG, path)
            strip = Strip()
            controller.device = strip
            with patch.object(controller.schedule, "current", return_value=("tonight", "sunrise", None)) as current:
                await controller.reconcile()
                self.assertEqual([outlet.is_on for outlet in strip.children], [True] + [False] * 5)

                async with TestClient(TestServer(make_app(controller))) as client:
                    self.assertEqual((await client.get("/")).status, 200)
                    response = await client.post("/api/lights/2", json={"on": True})
                    self.assertEqual(response.status, 200)
                    self.assertTrue((await response.json())["lights"][1]["on"])
                    self.assertTrue(strip.children[0].is_on)
                    self.assertFalse(strip.children[2].is_on)
                    self.assertEqual(json.loads(path.read_text())["lights"], {"2": True})

                    restarted = Controller(CONFIG, path)
                    restarted.device = strip
                    with patch.object(restarted.schedule, "current", return_value=current.return_value):
                        await strip.children[1].turn_off()
                        await restarted.reconcile()
                        self.assertTrue(strip.children[1].is_on)

                    current.return_value = (None, "sunset", None)
                    async with controller.lock:
                        await controller.reconcile()
                    self.assertFalse(any(outlet.is_on for outlet in strip.children))
                    response = await client.post("/api/lights/2", json={"on": True})
                    self.assertEqual(response.status, 409)

                    current.return_value = ("next night", "sunrise", None)
                    async with controller.lock:
                        await controller.reconcile()
                    self.assertEqual([outlet.is_on for outlet in strip.children], [True] + [False] * 5)


if __name__ == "__main__":
    unittest.main()

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

    def __init__(self, host="127.0.0.1", alias=None):
        self.host = host
        self.alias = alias
        self.children = [Outlet(True) for _ in range(6)]
        self.offline = False
        self.closed = False

    async def update(self, *, update_children):
        if self.offline:
            raise OSError("Strip went offline")

    async def disconnect(self):
        self.closed = True


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(CONFIG, Path(directory) / "state.json")
            app = make_app(controller, "prestons-mac-mini.local")
            async with TestClient(TestServer(app)) as client:
                for host, expected in (
                    ("prestons-mac-mini.local:8080", 200),
                    ("Prestons-Mac-Mini.LOCAL.:8080", 200),
                    ("localhost:8080", 200),
                    ("192.168.1.10:8080", 200),
                    ("other-device.local:8080", 400),
                    ("example.com:8080", 400),
                ):
                    with self.subTest(host=host):
                        async with client.get("/", headers={"Host": host}) as response:
                            self.assertEqual(response.status, expected)

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
            path.write_text(json.dumps({"rotation_enabled": False}))
            controller = Controller(CONFIG, path)
            strip = Strip()
            controller.strips["main"].device = strip
            with patch.object(controller.schedule, "current", return_value=("tonight", "sunrise", None)) as current:
                await controller.reconcile()
                self.assertEqual([outlet.is_on for outlet in strip.children], [True] + [False] * 5)

                async with TestClient(TestServer(make_app(controller))) as client:
                    self.assertEqual((await client.get("/")).status, 200)
                    with self.assertLogs("server", level="INFO") as logs:
                        response = await client.post("/api/lights/2", json={"on": True})
                    self.assertEqual(response.status, 200)
                    self.assertIn("Manual request for 'Porch' (main, outlet 2): on", "\n".join(logs.output))
                    self.assertIn("Changed HS300 '127.0.0.1' outlet 2: on", "\n".join(logs.output))
                    self.assertTrue((await response.json())["lights"][1]["on"])
                    self.assertTrue(strip.children[0].is_on)
                    self.assertFalse(strip.children[2].is_on)
                    self.assertEqual(json.loads(path.read_text())["lights"], {"2": True})

                    async with controller.lock:
                        with self.assertNoLogs("server", level="INFO"):
                            await controller.reconcile()
                        await strip.children[1].turn_off()
                        with self.assertLogs("server", level="INFO") as logs:
                            await controller.reconcile()
                    self.assertIn("Observed HS300 '127.0.0.1' outlet 2: off", "\n".join(logs.output))

                    restarted = Controller(CONFIG, path)
                    restarted.strips["main"].device = strip
                    with patch.object(restarted.schedule, "current", return_value=current.return_value):
                        await strip.children[1].turn_off()
                        await restarted.reconcile()
                        self.assertTrue(strip.children[1].is_on)

                    current.return_value = (None, "sunset", None)
                    async with controller.lock:
                        await controller.reconcile()
                    self.assertFalse(any(outlet.is_on for outlet in strip.children))
                    response = await client.post("/api/lights/2", json={"on": True})
                    self.assertEqual(response.status, 200)
                    self.assertTrue(strip.children[1].is_on)

                    current.return_value = ("next night", "sunrise", None)
                    async with controller.lock:
                        await controller.reconcile()
                    self.assertEqual([outlet.is_on for outlet in strip.children], [True] + [False] * 5)

    async def test_daytime_manual_lights_expire_independently(self):
        noon = datetime(2026, 11, 26, 18, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            controller = Controller(CONFIG, path)
            strip = Strip()
            controller.strips["main"].device = strip
            with patch("server.utc_now", return_value=noon) as clock:
                async with TestClient(TestServer(make_app(controller))) as client:
                    response = await client.post("/api/lights/1", json={"on": True})
                    self.assertEqual(response.status, 200)
                    status = await response.json()
                    self.assertFalse(status["night"])
                    self.assertTrue(status["lights"][0]["on"])
                    self.assertIsNone(status["rotation"]["next_at"])

                    clock.return_value = noon + timedelta(seconds=15)
                    response = await client.post("/api/lights/2", json={"on": True})
                    self.assertEqual(response.status, 200)

                    restarted = Controller(CONFIG, path)
                    restarted.strips["main"].device = strip
                    clock.return_value = noon + timedelta(seconds=299)
                    await restarted.reconcile()
                    self.assertEqual([outlet.is_on for outlet in strip.children[:2]], [True, True])

                    clock.return_value = noon + timedelta(minutes=5)
                    async with controller.lock:
                        await controller.reconcile()
                    self.assertEqual([outlet.is_on for outlet in strip.children[:2]], [False, True])

                    response = await client.post("/api/lights/2", json={"on": False})
                    self.assertEqual(response.status, 200)
                    self.assertFalse(strip.children[1].is_on)
                    self.assertNotIn("2", json.loads(path.read_text())["daytime_until"])

    async def test_random_rotation_across_strips_and_manual_countdown(self):
        config = {
            **{key: CONFIG[key] for key in ("latitude", "longitude", "timezone")},
            "strips": {"front": "127.0.0.1", "upstairs": "127.0.0.2"},
            "lights": {
                "peppermints": {"name": "Peppermints", "window": "Front bay",
                                "strip": "front", "port": 1, "on_start": True},
                "star": {"name": "Star", "window": "Landing",
                         "strip": "upstairs", "port": 1, "on_start": True},
                "snowflakes": {"name": "Snowflakes", "strip": "upstairs", "port": 2,
                               "on_start": False},
            },
        }
        sunset_at = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
        night = (sunset_at.isoformat(), "sunrise", None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            controller = Controller(config, path)
            front, upstairs = Strip(), Strip("127.0.0.2")
            controller.strips["front"].device = front
            controller.strips["upstairs"].device = upstairs
            with (
                patch.object(controller.schedule, "current", return_value=night) as phase,
                patch("server.utc_now", return_value=sunset_at) as clock,
                patch("server.random.sample", side_effect=lambda choices, count: choices[:count]) as pick,
            ):
                await controller.reconcile()
                self.assertTrue(front.children[0].is_on)
                self.assertTrue(upstairs.children[0].is_on)
                self.assertTrue(controller.status()["rotation"]["enabled"])
                self.assertEqual(controller.status()["rotation"]["next_at"], sunset_at.timestamp() + 300)

                async with TestClient(TestServer(make_app(controller))) as client:
                    response = await client.post("/api/rotation", json={"enabled": False})
                    self.assertEqual(response.status, 200)
                    self.assertFalse((await response.json())["rotation"]["enabled"])
                    self.assertFalse(Controller(config, path).status()["rotation"]["enabled"])
                    response = await client.post("/api/rotation/pick")
                    self.assertEqual(response.status, 409)

                    response = await client.post("/api/rotation", json={"enabled": True})
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        (await response.json())["rotation"]["next_at"], sunset_at.timestamp() + 300
                    )
                    clock.return_value = sunset_at + timedelta(seconds=299)
                    await controller.reconcile()
                    pick.assert_not_called()

                    clock.return_value = sunset_at + timedelta(minutes=5)
                    await controller.reconcile()
                    self.assertTrue(front.children[0].is_on)
                    self.assertTrue(upstairs.children[0].is_on)
                    self.assertFalse(upstairs.children[1].is_on)
                    self.assertEqual(controller.status()["rotation"]["active_lights"], [
                        {"name": "Peppermints", "window": "Front bay"},
                        {"name": "Star", "window": "Landing"},
                    ])

                    clock.return_value = sunset_at + timedelta(seconds=310)
                    response = await client.post("/api/lights/star", json={"on": True})
                    self.assertEqual(response.status, 200)
                    self.assertTrue(front.children[0].is_on)
                    self.assertTrue(upstairs.children[0].is_on)
                    state = (await response.json())["rotation"]
                    self.assertTrue(state["manual"])
                    self.assertEqual(state["next_at"], clock.return_value.timestamp() + 300)

                    clock.return_value = sunset_at + timedelta(seconds=330)
                    response = await client.post("/api/lights/star", json={"on": False})
                    self.assertEqual(response.status, 200)
                    self.assertFalse(upstairs.children[0].is_on)
                    self.assertEqual(
                        (await response.json())["rotation"]["next_at"],
                        clock.return_value.timestamp() + 300,
                    )

                    restarted = Controller(config, path)
                    restarted.strips["front"].device = front
                    restarted.strips["upstairs"].device = upstairs
                    with patch.object(restarted.schedule, "current", return_value=night):
                        clock.return_value = sunset_at + timedelta(seconds=629)
                        await restarted.reconcile()
                        self.assertTrue(front.children[0].is_on)
                        self.assertFalse(upstairs.children[0].is_on)

                        clock.return_value = sunset_at + timedelta(seconds=630)
                        await restarted.reconcile()
                        pick.assert_called_with(["peppermints", "star"], 1)
                        self.assertTrue(front.children[0].is_on)
                        self.assertFalse(upstairs.children[0].is_on)
                        self.assertTrue(upstairs.children[1].is_on)
                        self.assertEqual(restarted.status()["rotation"]["active"], "Snowflakes, Peppermints")

                    clock.return_value = sunset_at + timedelta(seconds=631)
                    response = await client.post("/api/rotation/pick")
                    self.assertEqual(response.status, 200)
                    rotation = (await response.json())["rotation"]
                    self.assertEqual(rotation["active"], "Snowflakes, Peppermints")
                    self.assertEqual(rotation["next_at"], clock.return_value.timestamp() + 300)
                    self.assertFalse(upstairs.children[0].is_on)
                    self.assertTrue(upstairs.children[1].is_on)

                    phase.return_value = (None, "sunset", None)
                    response = await client.post("/api/rotation/pick")
                    self.assertEqual(response.status, 409)
                    await controller.reconcile()
                    self.assertFalse(any(outlet.is_on for outlet in front.children + upstairs.children))
                    self.assertIsNone(controller.status()["rotation"]["next_at"])
                    phase.return_value = (
                        (sunset_at + timedelta(days=1)).isoformat(), "sunrise", None
                    )
                    clock.return_value = sunset_at + timedelta(days=1)
                    await controller.reconcile()
                    self.assertTrue(front.children[0].is_on)
                    self.assertTrue(upstairs.children[0].is_on)
                    self.assertEqual(
                        controller.status()["rotation"]["next_at"],
                        clock.return_value.timestamp() + 300,
                    )


    async def test_two_aliases_share_discovery_and_reconnect_at_new_ip(self):
        config = {
            **CONFIG,
            "strips": {"front": {"alias": "Front Windows"}, "upstairs": {"alias": "Upstairs"}},
            "lights": {
                "peppermints": {"name": "Peppermints", "strip": "front", "port": 1, "on_start": True},
                "star": {"name": "Star", "strip": "upstairs", "port": 1, "on_start": False},
            },
        }
        dusk = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
        front = Strip("127.0.0.1", "Front Windows")
        upstairs = Strip("127.0.0.2", "Upstairs")
        unrelated = Strip("127.0.0.3", "Other")
        moved = Strip("127.0.0.4", "Upstairs")
        duplicate = Strip("127.0.0.5", "Upstairs")

        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(config, Path(directory) / "state.json")
            with (
                patch.object(controller.schedule, "current", return_value=(dusk.isoformat(), "sunrise", None)),
                patch("server.utc_now", return_value=dusk),
                patch("server.Discover.discover", return_value={
                    strip.host: strip for strip in (front, upstairs, unrelated)
                }) as discover,
            ):
                await controller.reconcile()
                discover.assert_awaited_once()
                self.assertTrue(unrelated.closed)
                self.assertTrue(front.children[0].is_on)
                self.assertFalse(upstairs.children[0].is_on)

                async with TestClient(TestServer(make_app(controller))) as client:
                    response = await client.post("/api/lights/star", json={"on": True})
                    self.assertEqual(response.status, 200)
                    self.assertTrue(upstairs.children[0].is_on)
                    self.assertTrue(front.children[0].is_on)
                    discover.assert_awaited_once()

                    async with controller.lock:
                        upstairs.offline = True
                        await controller.reconcile()
                        self.assertTrue(upstairs.closed)
                        discover.return_value = {strip.host: strip for strip in (moved, duplicate)}
                        await controller.reconcile()
                        self.assertIn("More than one HS300", controller.status()["error"])
                        self.assertTrue(moved.closed)
                        self.assertTrue(duplicate.closed)

                        discover.return_value = {moved.host: moved}
                        await controller.reconcile()
                        self.assertEqual(controller.status()["error"], "")
                        self.assertIs(controller.strips["upstairs"].device, moved)
                        self.assertTrue(moved.children[0].is_on)
                        self.assertFalse(moved.children[1].is_on)


if __name__ == "__main__":
    unittest.main()

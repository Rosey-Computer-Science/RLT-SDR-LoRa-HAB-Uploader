import configparser
import json
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import urlopen
from urllib.request import Request

from web_interface import PacketWebServer, packet_from_sentence


NOW = datetime(2026, 9, 27, 12, 0, 5, tzinfo=timezone.utc)


class PacketFormattingTests(unittest.TestCase):
    def test_configured_names_and_position_are_extracted(self):
        packet = packet_from_sentence(
            "$$ROSEY-1,42,12:00:00,46.5191,6.5668,1234,9*ABCD",
            ["callsign", "frame", "time", "lat", "lon", "alt", "sats"],
            NOW,
            snr=-8.25,
        )
        self.assertEqual(packet["fields"]["callsign"], "ROSEY-1")
        self.assertEqual(packet["fields"]["sats"], "9")
        self.assertEqual(packet["lat"], 46.5191)
        self.assertEqual(packet["lon"], 6.5668)
        self.assertEqual(packet["alt"], 1234.0)
        self.assertEqual(packet["snr"], -8.25)

    def test_unnamed_extra_values_are_not_lost(self):
        packet = packet_from_sentence("ONE,TWO,THREE", ["first"], NOW)
        self.assertEqual(
            packet["fields"],
            {"first": "ONE", "field_2": "TWO", "field_3": "THREE"},
        )


class DashboardServerTests(unittest.TestCase):
    def test_dashboard_and_packet_api_are_served(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "telemetry": {"fields": "callsign, frame, time, lat, lon, alt"},
                "web": {"enabled": "true", "host": "127.0.0.1", "port": "0"},
            }
        )
        server = PacketWebServer(config, log=lambda _message: None)
        server.start()
        try:
            server.add_packet("ROSEY-1,1,12:00:00,46.5,6.5,1000", NOW, -7.0)
            with urlopen(server.url + "/api/packets", timeout=2) as response:
                feed = json.loads(response.read())
            with urlopen(server.url + "/", timeout=2) as response:
                html = response.read().decode("utf-8")
        finally:
            server.close()

        self.assertEqual(feed["total"], 1)
        self.assertEqual(feed["packets"][0]["fields"]["frame"], "1")
        self.assertIn("LoRa balloon tracker", html)

    def test_frequency_is_reported_and_can_be_changed(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "lora": {"frequency_mhz": "434.100"},
                "telemetry": {"fields": "callsign, frame"},
                "web": {"enabled": "true", "host": "127.0.0.1", "port": "0"},
            }
        )
        changes = []
        server = PacketWebServer(config, log=lambda _message: None, on_frequency_change=changes.append)
        server.start()
        try:
            with urlopen(server.url + "/api/packets", timeout=2) as response:
                initial = json.loads(response.read())
            request = Request(
                server.url + "/api/frequency",
                data=json.dumps({"frequency_mhz": 434.65}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2) as response:
                changed = json.loads(response.read())
        finally:
            server.close()

        self.assertEqual(initial["frequency_mhz"], 434.1)
        self.assertEqual(changed["frequency_mhz"], 434.65)
        self.assertEqual(changes, [434.65])

    def test_frequency_outside_rtl_sdr_range_is_rejected(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "lora": {"frequency_mhz": "434.100"},
                "telemetry": {"fields": "callsign, frame"},
                "web": {"enabled": "true", "host": "127.0.0.1", "port": "0"},
            }
        )
        changes = []
        server = PacketWebServer(config, log=lambda _message: None, on_frequency_change=changes.append)
        server.start()
        try:
            request = Request(
                server.url + "/api/frequency",
                data=json.dumps({"frequency_mhz": 10}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=2)
        finally:
            server.close()

        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(changes, [])


if __name__ == "__main__":
    unittest.main()

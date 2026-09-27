import configparser
import unittest
from datetime import datetime, timezone

from sondehub_uploader import SondeHubUploader, TelemetryError, crc16_ccitt, parse_telemetry


NOW = datetime(2026, 9, 27, 12, 0, 5, tzinfo=timezone.utc)


class TelemetryParsingTests(unittest.TestCase):
    def test_minimum_sentence(self):
        result = parse_telemetry(
            "$$ROSEY-1,42,12:00:00,46.5191,6.5668,1234",
            ["callsign", "frame", "time", "lat", "lon", "alt"],
            received_at=NOW,
        )
        self.assertEqual(result["payload_callsign"], "ROSEY-1")
        self.assertEqual(result["frame"], 42)
        self.assertEqual(result["datetime"], "2026-09-27T12:00:00.000000Z")
        self.assertEqual(result["lat"], 46.5191)
        self.assertEqual(result["alt"], 1234.0)

    def test_optional_fields_and_checksum(self):
        body = "ROSEY-1,43,12:00:03,46.5,6.5,1300,9,-12.5,4.10"
        sentence = f"$${body}*{crc16_ccitt(body):04X}"
        result = parse_telemetry(
            sentence,
            ["callsign", "frame", "time", "lat", "lon", "alt", "sats", "temp", "batt"],
            received_at=NOW,
            require_checksum=True,
        )
        self.assertEqual(result["sats"], 9)
        self.assertEqual(result["temp"], -12.5)
        self.assertEqual(result["batt"], 4.1)

    def test_bad_checksum_is_rejected(self):
        with self.assertRaisesRegex(TelemetryError, "checksum failed"):
            parse_telemetry(
                "$$ROSEY-1,1,12:00:00,46.5,6.5,1000*0000",
                ["callsign", "frame", "time", "lat", "lon", "alt"],
                received_at=NOW,
            )

    def test_stale_packet_is_rejected(self):
        with self.assertRaisesRegex(TelemetryError, "differs from this computer"):
            parse_telemetry(
                "ROSEY-1,1,11:00:00,46.5,6.5,1000",
                ["callsign", "frame", "time", "lat", "lon", "alt"],
                received_at=NOW,
                max_time_difference_seconds=300,
            )


class DisabledUploaderTests(unittest.TestCase):
    def test_disabled_uploader_does_not_start_worker(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "lora": {"frequency_mhz": "434.660"},
                "telemetry": {"fields": "callsign, frame, time, lat, lon, alt"},
                "sondehub": {"enabled": "false"},
            }
        )
        uploader = SondeHubUploader(config)
        self.assertFalse(uploader.enabled)
        self.assertIsNone(uploader._thread)


class EnabledUploaderTests(unittest.TestCase):
    def test_submit_builds_sondehub_payload(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "lora": {"frequency_mhz": "434.660"},
                "telemetry": {"fields": "callsign, frame, time, lat, lon, alt"},
                "sondehub": {
                    "enabled": "true",
                    "uploader_callsign": "SCHOOL-GS",
                    "dev_mode": "true",
                    "timeout_seconds": "1",
                    "retries": "1",
                },
            }
        )
        uploaded = []
        uploader = SondeHubUploader(config, log=lambda _message: None)
        uploader._put_json = lambda url, payload, compressed: uploaded.append(
            (url, payload, compressed)
        )
        uploader.submit("ROSEY-1,7,12:00:03,46.5,6.5,1300", received_at=NOW, snr=-7.5)
        uploader._queue.join()
        uploader.close()

        self.assertEqual(len(uploaded), 1)
        packet = uploaded[0][1][0]
        self.assertTrue(uploaded[0][2])
        self.assertEqual(packet["payload_callsign"], "ROSEY-1")
        self.assertEqual(packet["uploader_callsign"], "SCHOOL-GS")
        self.assertEqual(packet["frequency"], 434.660)
        self.assertEqual(packet["snr"], -7.5)
        self.assertEqual(packet["dev"], "testing")


if __name__ == "__main__":
    unittest.main()

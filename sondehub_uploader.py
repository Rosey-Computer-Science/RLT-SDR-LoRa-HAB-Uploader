"""Parse HAB telemetry and upload it to SondeHub Amateur.

Only Python's standard library is used so the receiver remains easy to install.
"""

from __future__ import annotations

import configparser
import gzip
import json
import math
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SOFTWARE_NAME = "rlt-sdr-lora-hab-uploader"
SOFTWARE_VERSION = "1.0.0"
DEFAULT_TELEMETRY_URL = "https://api.v2.sondehub.org/amateur/telemetry"
DEFAULT_LISTENER_URL = "https://api.v2.sondehub.org/amateur/listeners"


class TelemetryError(ValueError):
    """Raised when a received sentence cannot safely be uploaded."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def crc16_ccitt(text: str) -> int:
    """Return the UKHAS CRC-16-CCITT checksum for an ASCII sentence body."""
    crc = 0xFFFF
    for byte in text.encode("ascii"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _split_sentence(sentence: str, require_checksum: bool) -> tuple[str, Optional[str]]:
    cleaned = sentence.strip()
    if cleaned.startswith("$$"):
        cleaned = cleaned[2:]

    body, marker, checksum = cleaned.partition("*")
    if marker:
        if not re.fullmatch(r"[0-9A-Fa-f]{4}", checksum):
            raise TelemetryError("checksum must contain exactly four hexadecimal characters")
        expected = crc16_ccitt(body)
        if int(checksum, 16) != expected:
            raise TelemetryError(f"checksum failed (received {checksum.upper()}, expected {expected:04X})")
        return body, checksum.upper()

    if require_checksum:
        raise TelemetryError("sentence has no *FFFF checksum")
    return body, None


def _packet_datetime(value: str, received_at: datetime) -> datetime:
    value = value.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}(?:\.\d+)?", value):
        packet_time = datetime.strptime(value, "%H:%M:%S.%f" if "." in value else "%H:%M:%S").time()
        candidates = [
            datetime.combine((received_at + timedelta(days=offset)).date(), packet_time, timezone.utc)
            for offset in (-1, 0, 1)
        ]
        return min(candidates, key=lambda candidate: abs((candidate - received_at).total_seconds()))

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TelemetryError("time must be HH:MM:SS (UTC) or an ISO-8601 date/time") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_telemetry(
    sentence: str,
    fields: list[str],
    received_at: Optional[datetime] = None,
    require_checksum: bool = False,
    max_time_difference_seconds: int = 300,
) -> dict:
    """Convert a configurable CSV/UKHAS sentence into SondeHub fields."""
    received_at = (received_at or utc_now()).astimezone(timezone.utc)
    body, _ = _split_sentence(sentence, require_checksum)
    values = [value.strip() for value in body.split(",")]
    if len(values) != len(fields):
        raise TelemetryError(f"expected {len(fields)} CSV values but received {len(values)}")

    data = dict(zip(fields, values))
    required = ("callsign", "frame", "time", "lat", "lon", "alt")
    missing = [name for name in required if not data.get(name)]
    if missing:
        raise TelemetryError("missing required field(s): " + ", ".join(missing))

    try:
        frame = int(data["frame"])
        latitude = float(data["lat"])
        longitude = float(data["lon"])
        altitude = float(data["alt"])
    except ValueError as error:
        raise TelemetryError("frame, latitude, longitude and altitude must be numbers") from error

    if not all(math.isfinite(value) for value in (latitude, longitude, altitude)):
        raise TelemetryError("latitude, longitude and altitude must be finite numbers")
    if not -90 <= latitude <= 90:
        raise TelemetryError("latitude is outside -90 to 90 degrees")
    if not -180 <= longitude <= 180:
        raise TelemetryError("longitude is outside -180 to 180 degrees")

    packet_time = _packet_datetime(data["time"], received_at)
    time_difference = abs((packet_time - received_at).total_seconds())
    if max_time_difference_seconds > 0 and time_difference > max_time_difference_seconds:
        raise TelemetryError(
            f"payload time differs from this computer by {time_difference:.0f} seconds "
            f"(limit: {max_time_difference_seconds})"
        )

    callsign = data["callsign"].lstrip("$").strip()
    if not callsign:
        raise TelemetryError("callsign is empty")

    output = {
        "payload_callsign": callsign,
        "frame": frame,
        "datetime": iso_utc(packet_time),
        "lat": latitude,
        "lon": longitude,
        "alt": altitude,
        "raw": sentence.strip(),
    }

    float_fields = {
        "temp": "temp",
        "temperature": "temp",
        "humidity": "humidity",
        "pressure": "pressure",
        "battery": "batt",
        "batt": "batt",
        "speed": "speed",
        "vel_h": "vel_h",
        "vel_v": "vel_v",
        "heading": "heading",
    }
    for source_name, destination_name in float_fields.items():
        if data.get(source_name):
            try:
                output[destination_name] = float(data[source_name])
            except ValueError as error:
                raise TelemetryError(f"{source_name} must be a number") from error
    if data.get("sats"):
        try:
            output["sats"] = int(data["sats"])
        except ValueError as error:
            raise TelemetryError("sats must be a whole number") from error
    return output


def _optional_float(section: configparser.SectionProxy, key: str) -> Optional[float]:
    value = section.get(key, "").strip()
    return float(value) if value else None


class SondeHubUploader:
    """A non-blocking SondeHub Amateur uploader configured from config.ini."""

    def __init__(self, config: configparser.ConfigParser, log: Callable[[str], None] = print):
        settings = config["sondehub"]
        telemetry = config["telemetry"]
        self.enabled = settings.getboolean("enabled", fallback=False)
        self.uploader_callsign = settings.get("uploader_callsign", "").strip()
        self.antenna = settings.get("antenna", "").strip()
        self.radio = settings.get("radio", "RTL-SDR").strip()
        self.contact_email = settings.get("contact_email", "").strip()
        self.dev_mode = settings.getboolean("dev_mode", fallback=False)
        self.timeout = settings.getfloat("timeout_seconds", fallback=10.0)
        self.retries = max(1, settings.getint("retries", fallback=3))
        self.telemetry_url = settings.get("telemetry_url", DEFAULT_TELEMETRY_URL).strip()
        self.listener_url = settings.get("listener_url", DEFAULT_LISTENER_URL).strip()
        self.fields = [name.strip().lower() for name in telemetry.get("fields", "").split(",") if name.strip()]
        self.require_checksum = telemetry.getboolean("require_checksum", fallback=False)
        self.max_time_difference = telemetry.getint("max_time_difference_seconds", fallback=300)
        self.frequency_mhz = config["lora"].getfloat("frequency_mhz")
        self.log = log
        self.position = (
            _optional_float(settings, "station_latitude"),
            _optional_float(settings, "station_longitude"),
            _optional_float(settings, "station_altitude_m"),
        )
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None

        if self.enabled:
            if not self.uploader_callsign or self.uploader_callsign == "CHANGE_ME":
                raise ValueError("set [sondehub] uploader_callsign in config.ini before enabling uploads")
            if not self.fields:
                raise ValueError("set [telemetry] fields in config.ini before enabling uploads")
            supplied_position_values = sum(value is not None for value in self.position)
            if supplied_position_values not in (0, 3):
                raise ValueError(
                    "set all three SondeHub station position values, or leave all three blank"
                )
            self._thread = threading.Thread(target=self._worker, name="sondehub-uploader", daemon=True)
            self._thread.start()

    def submit(self, sentence: str, received_at: Optional[datetime] = None, snr: Optional[float] = None) -> None:
        """Validate a decoded sentence and queue it without blocking the receiver."""
        if not self.enabled:
            return
        received_at = (received_at or utc_now()).astimezone(timezone.utc)
        try:
            payload = parse_telemetry(
                sentence,
                self.fields,
                received_at=received_at,
                require_checksum=self.require_checksum,
                max_time_difference_seconds=self.max_time_difference,
            )
        except TelemetryError as error:
            self.log(f"SondeHub: not queued: {error}")
            return

        payload.update(
            {
                "software_name": SOFTWARE_NAME,
                "software_version": SOFTWARE_VERSION,
                "uploader_callsign": self.uploader_callsign,
                "time_received": iso_utc(received_at),
                "frequency": self.frequency_mhz,
                "uploader_radio": self.radio,
                "uploader_antenna": self.antenna,
            }
        )
        if snr is not None:
            payload["snr"] = snr
        if all(value is not None for value in self.position):
            payload["uploader_position"] = list(self.position)
        if self.dev_mode:
            payload["dev"] = "testing"
        self._queue.put(payload)
        self.log(f"SondeHub: queued {payload['payload_callsign']} frame {payload['frame']}")

    def _worker(self) -> None:
        if all(value is not None for value in self.position):
            self._upload_listener()
        while True:
            payload = self._queue.get()
            if payload is None:
                self._queue.task_done()
                return
            try:
                self._put_json(self.telemetry_url, [payload], compressed=True)
                self.log(f"SondeHub: uploaded {payload['payload_callsign']} frame {payload['frame']}")
            except Exception as error:  # Keep reception alive after any network/API failure.
                self.log(f"SondeHub: upload failed: {error}")
            finally:
                self._queue.task_done()

    def _upload_listener(self) -> None:
        payload = {
            "software_name": SOFTWARE_NAME,
            "software_version": SOFTWARE_VERSION,
            "uploader_callsign": self.uploader_callsign,
            "uploader_position": list(self.position),
            "uploader_radio": self.radio,
            "uploader_antenna": self.antenna,
            "uploader_contact_email": self.contact_email,
            "mobile": False,
        }
        try:
            self._put_json(self.listener_url, payload, compressed=False)
            self.log("SondeHub: uploaded receiver station details")
        except Exception as error:
            self.log(f"SondeHub: station upload failed: {error}")

    def _put_json(self, url: str, payload, compressed: bool) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "User-Agent": f"{SOFTWARE_NAME}-{SOFTWARE_VERSION}",
            "Content-Type": "application/json",
            "Date": formatdate(timeval=None, localtime=False, usegmt=True),
        }
        if compressed:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"

        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                request = Request(url, data=body, headers=headers, method="PUT")
                with urlopen(request, timeout=self.timeout) as response:
                    response_body = response.read().decode("utf-8", errors="replace")
                    if response.status not in (200, 202):
                        raise RuntimeError(f"HTTP {response.status}: {response_body}")
                    if response.status == 202 and response_body:
                        self.log(f"SondeHub: API response: {response_body}")
                    return
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"HTTP {error.code}: {detail}")
                if error.code < 500:
                    break
            except (URLError, TimeoutError, OSError) as error:
                last_error = error
            if attempt < self.retries:
                time.sleep(min(2 ** (attempt - 1), 4))
        raise RuntimeError(str(last_error or "unknown upload error"))

    def close(self) -> None:
        """Upload queued packets, then stop the worker thread."""
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout=max(5.0, self.timeout * self.retries + 1.0))

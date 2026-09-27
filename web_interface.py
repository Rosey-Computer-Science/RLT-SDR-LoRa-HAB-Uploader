"""Small local web interface for decoded LoRa HAB packets."""

from __future__ import annotations

import json
import math
import threading
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse


WEB_DIRECTORY = Path(__file__).resolve().parent / "web"
CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def packet_from_sentence(
    sentence: str,
    field_names: list[str],
    received_at: datetime,
    snr: Optional[float] = None,
) -> dict:
    """Give every CSV value a configured name for display in the browser."""
    cleaned = sentence.strip()
    body = cleaned[2:] if cleaned.startswith("$$") else cleaned
    body = body.split("*", 1)[0]
    values = [value.strip() for value in body.split(",")]

    fields = {}
    for index, value in enumerate(values):
        name = field_names[index] if index < len(field_names) else f"field_{index + 1}"
        fields[name] = value

    def finite_coordinate(name: str, minimum: float, maximum: float) -> Optional[float]:
        try:
            value = float(fields[name])
        except (KeyError, ValueError):
            return None
        return value if minimum <= value <= maximum else None

    latitude = finite_coordinate("lat", -90, 90)
    longitude = finite_coordinate("lon", -180, 180)
    altitude = None
    try:
        altitude = float(fields["alt"])
    except (KeyError, ValueError):
        pass

    received_at = received_at.astimezone(timezone.utc)
    return {
        "id": received_at.timestamp(),
        "received_at": received_at.isoformat().replace("+00:00", "Z"),
        "raw": cleaned,
        "fields": fields,
        "lat": latitude,
        "lon": longitude,
        "alt": altitude,
        "snr": snr,
    }


class PacketStore:
    def __init__(self, maximum: int):
        self._packets = deque(maxlen=maximum)
        self._total = 0
        self._lock = threading.Lock()

    def add(self, packet: dict) -> None:
        with self._lock:
            self._packets.append(packet)
            self._total += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"total": self._total, "packets": list(self._packets)}


class PacketWebServer:
    """Serve the dashboard and its in-memory packet feed on a background thread."""

    def __init__(
        self,
        config,
        log=print,
        on_frequency_change: Optional[Callable[[float], None]] = None,
    ):
        settings = config["web"] if "web" in config else {}
        telemetry = config["telemetry"]
        self.enabled = _getboolean(settings, "enabled", True)
        self.host = settings.get("host", "127.0.0.1")
        self.port = int(settings.get("port", 8080))
        self.field_names = [
            name.strip().lower()
            for name in telemetry.get("fields", "").split(",")
            if name.strip()
        ]
        self.store = PacketStore(maximum=max(1, int(settings.get("max_packets", 500))))
        self.log = log
        self._frequency_mhz = (
            config["lora"].getfloat("frequency_mhz") if "lora" in config else None
        )
        self._frequency_lock = threading.Lock()
        self._on_frequency_change = on_frequency_change
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        visible_host = "localhost" if self.host in ("127.0.0.1", "0.0.0.0", "::") else self.host
        port = self._server.server_port if self._server else self.port
        return f"http://{visible_host}:{port}"

    def start(self) -> None:
        if not self.enabled:
            return

        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/api/packets":
                    feed = dashboard.store.snapshot()
                    feed["frequency_mhz"] = dashboard.frequency_mhz
                    self._send_json(feed)
                    return
                files = {
                    "/": WEB_DIRECTORY / "index.html",
                    "/index.html": WEB_DIRECTORY / "index.html",
                    "/app.js": WEB_DIRECTORY / "app.js",
                    "/style.css": WEB_DIRECTORY / "style.css",
                }
                file_path = files.get(path)
                if file_path is None or not file_path.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content = file_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", CONTENT_TYPES[file_path.suffix])
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(content)

            def do_POST(self) -> None:
                if urlparse(self.path).path != "/api/frequency":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if self.headers.get_content_type() != "application/json":
                    self._send_json(
                        {"error": "Content-Type must be application/json"},
                        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    )
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 1024:
                        raise ValueError("request body is empty or too large")
                    value = json.loads(self.rfile.read(length))["frequency_mhz"]
                    frequency_mhz = float(value)
                    if not math.isfinite(frequency_mhz) or not 24 <= frequency_mhz <= 1766:
                        raise ValueError("frequency must be between 24 and 1766 MHz")
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return

                try:
                    dashboard.change_frequency(frequency_mhz)
                except Exception as error:
                    dashboard.log(f"Could not retune receiver: {error}")
                    self._send_json(
                        {"error": "receiver could not be retuned"},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._send_json({"frequency_mhz": dashboard.frequency_mhz})

            def _send_json(self, value, status=HTTPStatus.OK) -> None:
                content = json.dumps(value, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, _format: str, *_args) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="packet-web-interface",
            daemon=True,
        )
        self._thread.start()
        self.log(f"Web interface: {self.url}")

    @property
    def frequency_mhz(self) -> Optional[float]:
        with self._frequency_lock:
            return self._frequency_mhz

    def change_frequency(self, frequency_mhz: float) -> None:
        if self._on_frequency_change is None:
            raise RuntimeError("frequency changes are not enabled")
        self._on_frequency_change(frequency_mhz)
        with self._frequency_lock:
            self._frequency_mhz = frequency_mhz

    def add_packet(
        self,
        sentence: str,
        received_at: datetime,
        snr: Optional[float] = None,
    ) -> None:
        if self.enabled:
            self.store.add(packet_from_sentence(sentence, self.field_names, received_at, snr))

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def _getboolean(settings, key: str, fallback: bool) -> bool:
    if hasattr(settings, "getboolean"):
        return settings.getboolean(key, fallback=fallback)
    value = str(settings.get(key, fallback)).strip().lower()
    return value in ("1", "true", "yes", "on")

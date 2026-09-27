#!/usr/bin/env python3
"""Run lora_rx, display decoded telemetry, and optionally upload to SondeHub."""

import argparse
import configparser
import queue
import re
import shlex
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from sondehub_uploader import SondeHubUploader
from web_interface import PacketWebServer


SCRIPT_DIRECTORY = Path(__file__).resolve().parent

# UKHAS/Pi In The Sky LoRa modes 0-5. The integer bandwidth values are the
# closest representation of the radio's nominal 20.8 and 41.7 kHz settings.
LORA_MODES = {
    0: {"sf": 11, "bandwidth_hz": 20833, "coding_rate": 4, "implicit": False, "ldro": "on", "os": 12},
    1: {"sf": 6, "bandwidth_hz": 20833, "coding_rate": 1, "implicit": True, "ldro": "off", "os": 12},
    2: {"sf": 8, "bandwidth_hz": 62500, "coding_rate": 4, "implicit": False, "ldro": "off", "os": 4},
    3: {"sf": 7, "bandwidth_hz": 250000, "coding_rate": 2, "implicit": False, "ldro": "off", "os": 4},
    4: {"sf": 6, "bandwidth_hz": 250000, "coding_rate": 1, "implicit": True, "ldro": "off", "os": 4},
    5: {"sf": 11, "bandwidth_hz": 41667, "coding_rate": 4, "implicit": False, "ldro": "off", "os": 6},
}


def load_config(path: Path) -> configparser.ConfigParser:
    config = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if not config.read(path):
        raise FileNotFoundError(f"configuration file not found: {path}")
    for section in ("lora", "telemetry", "sondehub"):
        if section not in config:
            raise ValueError(f"missing [{section}] section in {path}")
    return config


def receiver_command(config: configparser.ConfigParser) -> list[str]:
    settings = config["lora"]
    try:
        mode_number = settings.getint("mode")
        mode = LORA_MODES[mode_number]
    except (ValueError, KeyError) as error:
        raise ValueError("[lora] mode must be a UKHAS LoRa mode from 0 to 5") from error

    frequency_hz = round(settings.getfloat("frequency_mhz") * 1_000_000)
    # Keep the SDR sample rate in its supported range and an exact multiple of
    # the preset bandwidth. Wider modes use 4x oversampling for sensitivity.
    sample_rate_hz = mode["bandwidth_hz"] * mode["os"]
    command = [
        str(SCRIPT_DIRECTORY / "lora_rx"),
        "-f", str(frequency_hz),
        "-s", str(sample_rate_hz),
        "-c", str(mode["coding_rate"]),
        "-w", settings.get("sync_word", "0x12"),
        "-p", str(settings.getint("frequency_correction_ppm", fallback=0)),
        "-g", str(settings.getint("gain_tenths_db", fallback=490)),
        "-d", settings.get("device", "0"),
        "-S", str(mode["sf"]),
        "-b", str(mode["bandwidth_hz"]),
        "-o", str(mode["ldro"]),
    ]
    if mode["implicit"]:
        command.extend(["-I", "-L", str(settings.getint("implicit_payload_length", fallback=255))])
    extra_arguments = settings.get("extra_arguments", "").strip()
    if extra_arguments:
        command.extend(shlex.split(extra_arguments))
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=SCRIPT_DIRECTORY / "config.ini",
        help="configuration file (default: config.ini beside this script)",
    )
    parser.add_argument("--show-command", action="store_true", help="print the lora_rx command and exit")
    args = parser.parse_args()

    try:
        config = load_config(args.config.expanduser().resolve())
        command = receiver_command(config)
    except (FileNotFoundError, ValueError, configparser.Error) as error:
        parser.error(str(error))

    if args.show_command:
        print(shlex.join(command))
        return 0

    if not Path(command[0]).is_file():
        parser.error("lora_rx has not been built; run 'make' first")

    uploader = None
    web_interface = None
    frequency_changes = queue.SimpleQueue()
    process_lock = threading.Lock()
    active_process = None

    def request_frequency_change(frequency_mhz: float) -> None:
        nonlocal active_process
        frequency_changes.put(frequency_mhz)
        with process_lock:
            if active_process is not None and active_process.poll() is None:
                active_process.terminate()

    try:
        uploader = SondeHubUploader(config)
        web_interface = PacketWebServer(config, on_frequency_change=request_frequency_change)
        web_interface.start()
    except (ValueError, configparser.Error) as error:
        if uploader is not None:
            uploader.close()
        parser.error(str(error))
    except OSError as error:
        if uploader is not None:
            uploader.close()
        if web_interface is not None:
            web_interface.close()
        parser.error(f"could not start web interface: {error}")

    print("SondeHub uploads are " + ("enabled.\n" if uploader.enabled else "disabled.\n"))

    latest_snr = None
    stopping = False
    try:
        while not stopping:
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as error:
                parser.error(f"could not start lora_rx: {error}")
            with process_lock:
                active_process = process
                # A web request can arrive between receiver restarts, while no
                # process exists for the request thread to terminate.
                if not frequency_changes.empty():
                    process.terminate()
            frequency_mhz = config["lora"].getfloat("frequency_mhz")
            print(f"Listening on {frequency_mhz:.3f} MHz. Press Control-C to stop.")

            assert process.stdout is not None
            for line in process.stdout:
                if line.startswith("rx cfg:"):
                    match = re.search(r"\bsnr=(-?\d+(?:\.\d+)?)", line)
                    latest_snr = float(match.group(1)) if match else None
                    continue
                if not line.startswith("rx ok:"):
                    continue

                received_at = datetime.now(timezone.utc)
                try:
                    payload = bytes.fromhex(line.split(":", 1)[1].strip())
                    message = payload.decode("ascii").strip("\x00\r\n ")
                except (ValueError, UnicodeDecodeError):
                    continue

                timestamp = received_at.astimezone().strftime("%H:%M:%S")
                print(f"[{timestamp}] {message}")
                web_interface.add_packet(message, received_at=received_at, snr=latest_snr)
                uploader.submit(message, received_at=received_at, snr=latest_snr)

            process.wait()
            with process_lock:
                active_process = None

            requested_frequency = None
            while True:
                try:
                    requested_frequency = frequency_changes.get_nowait()
                except queue.Empty:
                    break
            if requested_frequency is None:
                break

            config["lora"]["frequency_mhz"] = f"{requested_frequency:.6f}"
            uploader.frequency_mhz = requested_frequency
            command = receiver_command(config)
            latest_snr = None
            print(f"Retuning receiver to {requested_frequency:.6f} MHz...")

    except KeyboardInterrupt:
        stopping = True
        print("\nStopping...")
    finally:
        with process_lock:
            process = active_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        uploader.close()
        web_interface.close()
    print("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

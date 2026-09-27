import configparser
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parent / "decode-lora.py"
SPEC = importlib.util.spec_from_file_location("decode_lora", MODULE_PATH)
decode_lora = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(decode_lora)


EXPECTED = {
    0: {"sf": "11", "bw": "20833", "cr": "4", "sample_rate": "249996", "ldro": "on", "implicit": False},
    1: {"sf": "6", "bw": "20833", "cr": "1", "sample_rate": "249996", "ldro": "off", "implicit": True},
    2: {"sf": "8", "bw": "62500", "cr": "4", "sample_rate": "250000", "ldro": "off", "implicit": False},
    3: {"sf": "7", "bw": "250000", "cr": "2", "sample_rate": "1000000", "ldro": "off", "implicit": False},
    4: {"sf": "6", "bw": "250000", "cr": "1", "sample_rate": "1000000", "ldro": "off", "implicit": True},
    5: {"sf": "11", "bw": "41667", "cr": "4", "sample_rate": "250002", "ldro": "off", "implicit": False},
}


def config_for_mode(mode: int) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read_dict(
        {
            "lora": {
                "mode": str(mode),
                "frequency_mhz": "434.660",
                "sync_word": "0x12",
                "frequency_correction_ppm": "-3",
                "gain_tenths_db": "490",
                "device": "0",
                "implicit_payload_length": "255",
            }
        }
    )
    return config


def option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


class LoRaModeTests(unittest.TestCase):
    def test_modes_zero_to_five_expand_to_the_standard_parameters(self):
        for mode_number, expected in EXPECTED.items():
            with self.subTest(mode=mode_number):
                command = decode_lora.receiver_command(config_for_mode(mode_number))
                self.assertEqual(option(command, "-S"), expected["sf"])
                self.assertEqual(option(command, "-b"), expected["bw"])
                self.assertEqual(option(command, "-c"), expected["cr"])
                self.assertEqual(option(command, "-s"), expected["sample_rate"])
                self.assertEqual(option(command, "-o"), expected["ldro"])
                self.assertEqual("-I" in command, expected["implicit"])
                if expected["implicit"]:
                    self.assertEqual(option(command, "-L"), "255")

    def test_invalid_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "from 0 to 5"):
            decode_lora.receiver_command(config_for_mode(6))


if __name__ == "__main__":
    unittest.main()

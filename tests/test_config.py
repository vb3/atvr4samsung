"""Unit tests for config parsing/validation.

Tests target ``Config.from_mapping`` (the pure path), so they need neither PyYAML nor a file on
disk. Validation, defaults, and ``~`` expansion are the behaviors worth protecting.
"""
import os
import unittest
from pathlib import Path

from atvr4samsung.config import Config


def minimal_mapping(**overrides):
    data = {
        "companion": {"device_name": "Frame Living Room", "pin": "0000"},
        "samsung": {"host": "192.168.1.50", "mac": "aa:bb:cc:dd:ee:ff"},
    }
    data.update(overrides)
    return data


class TestConfigValidation(unittest.TestCase):
    def test_minimal_valid_config(self):
        cfg = Config.from_mapping(minimal_mapping())
        self.assertEqual(cfg.samsung.host, "192.168.1.50")
        self.assertEqual(cfg.samsung.mac, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(cfg.companion.device_name, "Frame Living Room")

    def test_missing_samsung_host_raises(self):
        data = minimal_mapping(samsung={"mac": "aa:bb:cc:dd:ee:ff"})
        with self.assertRaises(ValueError):
            Config.from_mapping(data)

    def test_missing_samsung_mac_raises(self):
        # MAC is required because Wake-on-LAN needs it.
        data = minimal_mapping(samsung={"host": "192.168.1.50"})
        with self.assertRaises(ValueError):
            Config.from_mapping(data)

    def test_empty_mapping_raises(self):
        with self.assertRaises(ValueError):
            Config.from_mapping({})

    def test_non_numeric_companion_pin_raises(self):
        data = minimal_mapping(companion={"device_name": "Frame Living Room", "pin": "abcd"})
        with self.assertRaisesRegex(ValueError, "config: companion.pin must be 4-8 digits"):
            Config.from_mapping(data)

    def test_too_short_companion_pin_raises(self):
        data = minimal_mapping(companion={"device_name": "Frame Living Room", "pin": "12"})
        with self.assertRaisesRegex(ValueError, "config: companion.pin must be 4-8 digits"):
            Config.from_mapping(data)

    def test_valid_companion_pin_is_accepted(self):
        cfg = Config.from_mapping(
            minimal_mapping(companion={"device_name": "Frame Living Room", "pin": "1337"})
        )
        self.assertEqual(cfg.companion.pin, "1337")

    def test_default_companion_pin_is_accepted(self):
        cfg = Config.from_mapping(minimal_mapping(companion={"device_name": "Frame Living Room"}))
        self.assertEqual(cfg.companion.pin, "0000")


class TestConfigDefaults(unittest.TestCase):
    def test_samsung_and_wol_defaults(self):
        cfg = Config.from_mapping(minimal_mapping())
        self.assertEqual(cfg.samsung.port, 8002)
        self.assertEqual(cfg.samsung.name, "atvr4samsung")
        self.assertTrue(cfg.samsung.wol.enabled)
        self.assertEqual(cfg.samsung.wol.port, 9)

    def test_companion_defaults(self):
        cfg = Config.from_mapping(minimal_mapping())
        self.assertEqual(cfg.companion.port, 49152)
        self.assertEqual(cfg.companion.model, "AppleTV14,1")

    def test_log_level_default_and_override(self):
        self.assertEqual(Config.from_mapping(minimal_mapping()).log_level, "INFO")
        cfg = Config.from_mapping(minimal_mapping(logging={"level": "DEBUG"}))
        self.assertEqual(cfg.log_level, "DEBUG")

    def test_types_are_coerced(self):
        # YAML may yield strings/ints loosely; ensure ints are ints.
        data = minimal_mapping(samsung={
            "host": "192.168.1.50",
            "mac": "aa:bb:cc:dd:ee:ff",
            "port": "8002",
            "wol": {"port": "9", "enabled": 1},
        })
        cfg = Config.from_mapping(data)
        self.assertIsInstance(cfg.samsung.port, int)
        self.assertEqual(cfg.samsung.port, 8002)
        self.assertIsInstance(cfg.samsung.wol.port, int)


class TestConfigCoercion(unittest.TestCase):
    def test_wol_enabled_string_false_is_false(self):
        data = minimal_mapping()
        data["samsung"]["wol"] = {"enabled": "false"}
        cfg = Config.from_mapping(data)
        self.assertFalse(cfg.samsung.wol.enabled)

    def test_wol_enabled_string_yes_is_true(self):
        data = minimal_mapping()
        data["samsung"]["wol"] = {"enabled": "yes"}
        cfg = Config.from_mapping(data)
        self.assertTrue(cfg.samsung.wol.enabled)

    def test_wol_enabled_zero_is_false(self):
        data = minimal_mapping()
        data["samsung"]["wol"] = {"enabled": 0}
        cfg = Config.from_mapping(data)
        self.assertFalse(cfg.samsung.wol.enabled)

    def test_invalid_wol_enabled_raises(self):
        data = minimal_mapping()
        data["samsung"]["wol"] = {"enabled": "maybe"}
        with self.assertRaisesRegex(ValueError, "config: invalid boolean"):
            Config.from_mapping(data)

    def test_out_of_range_samsung_port_raises(self):
        data = minimal_mapping()
        data["samsung"]["port"] = 70000
        with self.assertRaisesRegex(ValueError, "config: samsung.port must be 1-65535"):
            Config.from_mapping(data)

    def test_out_of_range_wol_port_raises(self):
        data = minimal_mapping()
        data["samsung"]["wol"] = {"port": 0}
        with self.assertRaisesRegex(ValueError, "config: samsung.wol.port must be 1-65535"):
            Config.from_mapping(data)


class TestPathExpansion(unittest.TestCase):
    def test_tilde_paths_are_expanded(self):
        data = minimal_mapping()
        data["samsung"]["token_file"] = "~/.local/state/atvr4samsung/samsung-token.txt"
        data["companion"]["state_dir"] = "~/.local/state/atvr4samsung"
        cfg = Config.from_mapping(data)
        self.assertIsInstance(cfg.samsung.token_file, Path)
        self.assertNotIn("~", str(cfg.samsung.token_file))
        self.assertTrue(str(cfg.samsung.token_file).startswith(os.path.expanduser("~")))
        self.assertNotIn("~", str(cfg.companion.state_dir))

    def test_unset_optional_paths_are_none(self):
        cfg = Config.from_mapping(minimal_mapping())
        self.assertIsNone(cfg.samsung.token_file)
        self.assertIsNone(cfg.companion.state_dir)


if __name__ == "__main__":
    unittest.main()

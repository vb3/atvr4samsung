"""Pure key-mapping tests: no pyatv, network, or TV dependencies."""
import unittest

from atvr4samsung.bridge.keymap import (
    GESTURE_TO_SAMSUNG,
    Action,
    AppleButton,
    resolve,
)


class TestKeymapResolution(unittest.TestCase):
    def test_mvp_directional_and_basic_keys(self):
        expected = {
            AppleButton.Up: "KEY_UP",
            AppleButton.Down: "KEY_DOWN",
            AppleButton.Left: "KEY_LEFT",
            AppleButton.Right: "KEY_RIGHT",
            AppleButton.Menu: "KEY_RETURN",
            AppleButton.Select: "KEY_ENTER",
            AppleButton.Home: "KEY_HOME",
            AppleButton.VolumeUp: "KEY_VOLUP",
            AppleButton.VolumeDown: "KEY_VOLDOWN",
            AppleButton.Mute: "KEY_MUTE",      # iOS 26 Control Center Mute = _hidC 18
            AppleButton.Power: "KEY_POWER",    # iOS 26 Control Center Power = _hidC 19
        }
        for button, key in expected.items():
            mapping = resolve(int(button))
            self.assertEqual(mapping.action, Action.SEND_KEY, button)
            self.assertEqual(mapping.samsung_key, key, button)
            self.assertTrue(mapping.mvp, f"{button} should be in the MVP set")

    def test_play_pause_is_a_single_toggle_key(self):
        # KEY_PLAY_BACK is a real single play/pause toggle on the Frame (confirmed against the real TV),
        # so we send it as one stateless key — no internal play-state model to drift out of sync.
        mapping = resolve(int(AppleButton.PlayPause))
        self.assertEqual(mapping.action, Action.SEND_KEY)
        self.assertEqual(mapping.samsung_key, "KEY_PLAY_BACK")
        self.assertTrue(mapping.mvp)

    def test_sleep_powers_off_and_wake_uses_wol(self):
        sleep = resolve(int(AppleButton.Sleep))
        self.assertEqual(sleep.action, Action.POWER_OFF)
        self.assertEqual(sleep.samsung_key, "KEY_POWER")

        wake = resolve(int(AppleButton.Wake))
        self.assertEqual(wake.action, Action.WAKE_ON_LAN)
        self.assertIsNone(wake.samsung_key, "WoL is a magic packet, not a TV key")

    def test_stretch_buttons_are_mapped_but_not_mvp(self):
        for button, key in (
            (AppleButton.ChannelIncrement, "KEY_CHUP"),
            (AppleButton.ChannelDecrement, "KEY_CHDOWN"),
            (AppleButton.Guide, "KEY_GUIDE"),
        ):
            mapping = resolve(int(button))
            self.assertEqual(mapping.action, Action.SEND_KEY, button)
            self.assertEqual(mapping.samsung_key, key, button)
            self.assertFalse(mapping.mvp, f"{button} is a stretch goal, not MVP")

    def test_siri_is_deliberately_unmapped(self):
        mapping = resolve(int(AppleButton.Siri))
        self.assertEqual(mapping.action, Action.UNMAPPED)
        self.assertIsNone(mapping.samsung_key)

    def test_ios26_cc_volume_mute_power_wire_codes(self):
        # iOS 26 Control Center sends Mute/Power on repurposed HID page codes 18/19 (NOT 29/30).
        # Pin the raw wire codes regardless of the AppleButton member names.
        self.assertEqual(resolve(8).samsung_key, "KEY_VOLUP")
        self.assertEqual(resolve(9).samsung_key, "KEY_VOLDOWN")
        self.assertEqual(resolve(18).samsung_key, "KEY_MUTE")   # CC Mute (HID "PageUp")
        self.assertEqual(resolve(19).samsung_key, "KEY_POWER")  # CC Power (HID "PageDown")

    def test_unknown_and_unhandled_codes_resolve_gracefully(self):
        # Unknown integers (future / malformed frames) must not raise. 29/30 are the decompile's
        # Mute/Power button *identifiers*, which iOS 26 never sends on the wire -> deliberately UNMAPPED.
        for code in (0, 29, 30, 99, 255, -1):
            mapping = resolve(code)
            self.assertEqual(mapping.action, Action.UNMAPPED, code)
            self.assertIsNone(mapping.samsung_key, code)

    def test_send_key_mappings_never_have_empty_key(self):
        from atvr4samsung.bridge.keymap import KEYMAP

        for button, mapping in KEYMAP.items():
            if mapping.action is Action.SEND_KEY:
                self.assertTrue(mapping.samsung_key, f"{button} SEND_KEY missing key")
                self.assertTrue(mapping.samsung_key.startswith("KEY_"), button)
            elif mapping.action in (Action.WAKE_ON_LAN, Action.UNMAPPED):
                self.assertIsNone(mapping.samsung_key, f"{button} should carry no raw key")


class TestGestureSamsungMap(unittest.TestCase):
    def test_all_directions_map_to_keys(self):
        self.assertEqual(
            GESTURE_TO_SAMSUNG,
            {
                "UP": "KEY_UP",
                "DOWN": "KEY_DOWN",
                "LEFT": "KEY_LEFT",
                "RIGHT": "KEY_RIGHT",
                "SELECT": "KEY_ENTER",
            },
        )


if __name__ == "__main__":
    unittest.main()

"""Unit tests for pairing PIN strength nudges."""
import unittest

from atvr4samsung.config import pin_is_weak


class TestPinWeakness(unittest.TestCase):
    def test_weak_pins(self):
        for pin in ("", "0", "000", "0000", "1111", "1234", "1337", "4321"):
            self.assertTrue(pin_is_weak(pin), pin)

    def test_stronger_pins(self):
        for pin in ("8472", "93715", "go-bananas-42"):
            self.assertFalse(pin_is_weak(pin), pin)


if __name__ == "__main__":
    unittest.main()

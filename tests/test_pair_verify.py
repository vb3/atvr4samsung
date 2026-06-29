"""Pair-verify authorization tests: only PIN-paired clients with a valid signature get a session.

Regression coverage for the fail-open where an empty paired store accepted ANY client, and for the
proof being read from the wrong message. These exercise our security decision directly, using the
same crypto/TLV primitives the protocol uses (no iPhone, no network).
"""
import os
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atvr4samsung.companion.protocol import chacha20
from atvr4samsung.companion.protocol.auth import CompanionServerAuth
from atvr4samsung.companion.protocol.paired_clients import PairedClients
from atvr4samsung.companion.protocol.tlv8 import TlvValue, write_tlv


class _Auth(CompanionServerAuth):
    def send_to_client(self, frame_type, data):  # pragma: no cover - unused in these tests
        pass

    def enable_encryption(self, output_key, input_key):  # pragma: no cover - unused
        pass


def _ltpk(sk: Ed25519PrivateKey) -> bytes:
    return sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


class TestPairVerifyEnforcement(unittest.TestCase):
    def setUp(self):
        self.session_key = os.urandom(32)
        self.client_pub = os.urandom(32)  # client ephemeral X25519 (M1)
        self.server_pub = os.urandom(32)  # server ephemeral X25519 (M2)

    def _auth(self, paired, *, require_paired=True):
        auth = _Auth("dev", paired_clients=paired, require_paired=require_paired)
        auth._pv_session_key = self.session_key
        auth._pv_client_pub = self.client_pub
        auth._pv_server_pub = self.server_pub
        return auth

    def _m3(self, identifier: bytes, signature: bytes) -> dict:
        """Build the M3 pairing_data: encrypted {Identifier, Signature} under the verify session key."""
        tlv = write_tlv({TlvValue.Identifier: identifier, TlvValue.Signature: signature})
        enc = chacha20.Chacha20Cipher(self.session_key, self.session_key).encrypt(
            tlv, nonce="PV-Msg03".encode()
        )
        return {TlvValue.EncryptedData: enc}

    def _sign(self, sk: Ed25519PrivateKey, identifier: bytes) -> bytes:
        # HAP: controller signs clientEphemeralPK || pairingID || serverEphemeralPK
        return sk.sign(self.client_pub + identifier + self.server_pub)

    def test_paired_client_with_valid_signature_is_accepted(self):
        sk = Ed25519PrivateKey.generate()
        ident = b"CLIENT-A"
        paired = PairedClients(None)
        paired.add(ident.decode(), _ltpk(sk))
        auth = self._auth(paired)
        self.assertTrue(auth._verify_client(self._m3(ident, self._sign(sk, ident))))

    def test_empty_store_is_rejected(self):
        # Regression: an empty store used to fail OPEN (return True for anyone).
        sk = Ed25519PrivateKey.generate()
        ident = b"CLIENT-A"
        auth = self._auth(PairedClients(None))
        self.assertFalse(auth._verify_client(self._m3(ident, self._sign(sk, ident))))

    def test_unknown_client_is_rejected(self):
        sk = Ed25519PrivateKey.generate()
        paired = PairedClients(None)
        paired.add("SOMEONE-ELSE", _ltpk(Ed25519PrivateKey.generate()))
        auth = self._auth(paired)
        ident = b"CLIENT-A"
        self.assertFalse(auth._verify_client(self._m3(ident, self._sign(sk, ident))))

    def test_bad_signature_is_rejected(self):
        sk = Ed25519PrivateKey.generate()
        ident = b"CLIENT-A"
        paired = PairedClients(None)
        paired.add(ident.decode(), _ltpk(sk))
        auth = self._auth(paired)
        wrong = Ed25519PrivateKey.generate()  # not the stored LTPK
        self.assertFalse(auth._verify_client(self._m3(ident, self._sign(wrong, ident))))

    def test_enforcement_disabled_accepts_without_a_store(self):
        auth = self._auth(PairedClients(None), require_paired=False)
        self.assertTrue(auth._verify_client({}))


class _RecordingAuth(CompanionServerAuth):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.encryption_enabled = 0

    def send_to_client(self, frame_type, data):
        pass

    def enable_encryption(self, output_key, input_key):
        self.encryption_enabled += 1


class TestPairVerifyMessages(unittest.TestCase):
    """M1/M3 message-level behavior: fresh server ephemeral, and encryption only on authorization."""

    def setUp(self):
        self.session_key = os.urandom(32)
        self.client_pub = os.urandom(32)
        self.server_pub = os.urandom(32)

    @staticmethod
    def _client_ephemeral() -> bytes:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        return X25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def _m3(self, identifier: bytes, signature: bytes) -> dict:
        tlv = write_tlv({TlvValue.Identifier: identifier, TlvValue.Signature: signature})
        enc = chacha20.Chacha20Cipher(self.session_key, self.session_key).encrypt(
            tlv, nonce="PV-Msg03".encode()
        )
        return {TlvValue.EncryptedData: enc}

    def _prime(self, auth):
        auth._pv_session_key = self.session_key
        auth._pv_client_pub = self.client_pub
        auth._pv_server_pub = self.server_pub

    def test_m1_uses_a_fresh_server_ephemeral_each_session(self):
        auth = _Auth("dev", paired_clients=PairedClients(None), require_paired=True)
        auth._m1_verify({TlvValue.PublicKey: self._client_ephemeral()})
        first = auth._pv_server_pub
        auth._m1_verify({TlvValue.PublicKey: self._client_ephemeral()})
        second = auth._pv_server_pub
        self.assertEqual(len(first), 32)
        self.assertNotEqual(first, second)  # static key would repeat -> replayable

    def test_m3_enables_encryption_for_an_authorized_client(self):
        sk = Ed25519PrivateKey.generate()
        ident = b"CLIENT-A"
        paired = PairedClients(None)
        paired.add(ident.decode(), _ltpk(sk))
        auth = _RecordingAuth("dev", paired_clients=paired, require_paired=True)
        self._prime(auth)
        sig = sk.sign(self.client_pub + ident + self.server_pub)
        auth._m3_verify(self._m3(ident, sig))
        self.assertEqual(auth.encryption_enabled, 1)

    def test_m3_does_not_enable_encryption_for_empty_store(self):
        sk = Ed25519PrivateKey.generate()
        ident = b"CLIENT-A"
        auth = _RecordingAuth("dev", paired_clients=PairedClients(None), require_paired=True)
        self._prime(auth)
        sig = sk.sign(self.client_pub + ident + self.server_pub)
        auth._m3_verify(self._m3(ident, sig))
        self.assertEqual(auth.encryption_enabled, 0)

    def test_m3_without_m1_fails_closed(self):
        # _pv_* unset (M3 arrived without M1) must reject, not raise.
        auth = _RecordingAuth("dev", paired_clients=PairedClients(None), require_paired=True)
        auth._m3_verify({TlvValue.EncryptedData: b"garbage"})
        self.assertEqual(auth.encryption_enabled, 0)


if __name__ == "__main__":
    unittest.main()

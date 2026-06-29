"""Companion server-side pairing (SRP pair-setup + verify) mixin. Origin: pyatv v0.18.0 (MIT)."""

from abc import ABC, abstractmethod
import binascii
from collections import namedtuple
import hashlib
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from srptools import SRPContext, SRPServerSession, constants

from .support import hkdf_expand
from .tlv8 import ErrorCode, TlvValue, read_tlv, write_tlv
from .identity import PIN_CODE, PRIVATE_KEY, SERVER_IDENTIFIER
from .enums import FrameType
from . import chacha20, opack

_LOGGER = logging.getLogger(__name__)

ServerKeys = namedtuple("ServerKeys", "sign auth auth_pub verify verify_pub")


def generate_keys(seed):
    signing_key = Ed25519PrivateKey.from_private_bytes(seed)
    verify_private = X25519PrivateKey.from_private_bytes(seed)
    return ServerKeys(
        sign=signing_key,
        auth=signing_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        auth_pub=signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        ),
        verify=verify_private,
        verify_pub=verify_private.public_key(),
    )


def new_server_session(keys, pin):
    context = SRPContext(
        "Pair-Setup",
        str(pin),
        prime=constants.PRIME_3072,
        generator=constants.PRIME_3072_GEN,
        hash_func=hashlib.sha512,
        bits_salt=128,
    )
    username, verifier, salt = context.get_user_data_triplet()

    context_server = SRPContext(
        username,
        prime=constants.PRIME_3072,
        generator=constants.PRIME_3072_GEN,
        hash_func=hashlib.sha512,
        bits_salt=128,
    )

    session = SRPServerSession(
        context_server, verifier, binascii.hexlify(keys.auth).decode()
    )

    return session, salt


def verify_controller_signature(srp_session_key: bytes, identifier: bytes, ltpk: bytes, signature: bytes) -> bool:
    """Verify the iOS controller's pair-setup M5 signature (HAP).

    The controller signs ``iOSDeviceX || iOSDevicePairingID || iOSDeviceLTPK`` with its long-term
    Ed25519 key, where ``iOSDeviceX`` is HKDF over the SRP shared secret with the controller-sign
    salt/info (mirror of the accessory M6 construction). Returns False on any failure (bad signature,
    wrong-length key, etc.) so callers fail closed.
    """
    try:
        ios_device_x = hkdf_expand(
            "Pair-Setup-Controller-Sign-Salt", "Pair-Setup-Controller-Sign-Info", srp_session_key
        )
        Ed25519PublicKey.from_public_bytes(ltpk).verify(signature, ios_device_x + identifier + ltpk)
        return True
    except Exception:
        return False


class CompanionServerAuth(ABC):
    def __init__(self, device_name, unique_id=SERVER_IDENTIFIER, pin=PIN_CODE, private_key=PRIVATE_KEY,
                 paired_clients=None, require_paired=False):
        self.device_name = device_name
        self.unique_id = unique_id.encode()
        self.input_key = None
        self.output_key = None
        self.keys = generate_keys(private_key)
        self.session, self.salt = new_server_session(self.keys, str(pin))
        # Persist client LTPKs at setup and verify their signature at pair-verify, so only PIN-paired
        # clients can connect. Disabled (legacy permissive) when no store is provided.
        self._paired = paired_clients
        self._require_paired = require_paired and paired_clients is not None

    def handle_auth_frame(self, frame_type, data):
        pairing_data = read_tlv(data["_pd"])
        seqno = int.from_bytes(pairing_data[TlvValue.SeqNo], byteorder="little")
        _LOGGER.debug("Received auth frame: type=%s, seqno=%s", frame_type, seqno)

        suffix = (
            "verify"
            if frame_type in [FrameType.PV_Start, FrameType.PV_Next]
            else "setup"
        )
        getattr(self, f"_m{seqno}_{suffix}")(pairing_data)

    def _m1_verify(self, pairing_data):
        # Fresh ephemeral X25519 per verify session (HAP requirement). A STATIC server ECDH key would
        # make the session key a function of only the client's ephemeral input, so a replayed M1
        # reproduces the same keys + nonce counters and lets recorded encrypted frames be replayed.
        # Our long-term Ed25519 identity key (self.keys.sign) still signs M2 to prove who we are.
        verify_private = X25519PrivateKey.generate()
        server_pub_key = verify_private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        client_pub_key = pairing_data[TlvValue.PublicKey]

        shared_key = verify_private.exchange(
            X25519PublicKey.from_public_bytes(client_pub_key)
        )

        session_key = hkdf_expand(
            "Pair-Verify-Encrypt-Salt", "Pair-Verify-Encrypt-Info", shared_key
        )

        info = server_pub_key + self.unique_id + client_pub_key
        signature = self.keys.sign.sign(info)

        tlv = write_tlv(
            {TlvValue.Identifier: self.unique_id, TlvValue.Signature: signature}
        )

        chacha = chacha20.Chacha20Cipher(session_key, session_key)
        encrypted = chacha.encrypt(tlv, nonce="PV-Msg02".encode())

        tlv = write_tlv(
            {
                TlvValue.SeqNo: b"\x02",
                TlvValue.PublicKey: server_pub_key,
                TlvValue.EncryptedData: encrypted,
            }
        )

        self.output_key = hkdf_expand("", "ServerEncrypt-main", shared_key)
        self.input_key = hkdf_expand("", "ClientEncrypt-main", shared_key)

        # Never log derived session keys; see SECURITY.md.

        # The client's proof (its pairing identifier + signature) arrives encrypted in M3, not here,
        # so stash the verify-session material and do the paired-client check in _m3_verify. Verifying
        # M1 (which only carries the client's ephemeral public key) can never see the proof.
        self._pv_session_key = session_key
        self._pv_server_pub = server_pub_key
        self._pv_client_pub = client_pub_key

        self.send_to_client(FrameType.PV_Next, {"_pd": tlv})

    def _verify_client(self, pairing_data) -> bool:
        """Authorize an M3 pair-verify: the client must be PIN-paired with a valid signature.

        Fails closed when ``require_paired`` is on: an empty store, an unknown client, or a bad
        signature all reject (no encryption enabled). Pair-setup records the client's long-term key
        before it ever runs verify, so a legitimate client is always present here; an empty store
        means nobody PIN-paired (or it was cleared/lost) — precisely when a session must NOT be
        granted. iOS responds to the rejection by falling back to PIN pair-setup.
        """
        if not self._require_paired:
            return True  # no paired store configured -> enforcement disabled (legacy permissive)
        try:
            chacha = chacha20.Chacha20Cipher(self._pv_session_key, self._pv_session_key)
            sub = read_tlv(chacha.decrypt(pairing_data[TlvValue.EncryptedData], nonce="PV-Msg03".encode()))
            identifier = sub[TlvValue.Identifier]  # raw bytes: iOS signs over these exact bytes
            identifier_str = identifier.decode("utf-8")  # strict: non-UTF-8 raises -> fail closed below
            ltpk = self._paired.ltpk(identifier_str)
            if ltpk is None:
                _LOGGER.warning("Pair-verify from UNKNOWN client %s — rejecting", identifier_str)
                return False
            # HAP construction: the controller signs clientEphemeralPK || pairingID || serverEphemeralPK.
            Ed25519PublicKey.from_public_bytes(ltpk).verify(
                sub[TlvValue.Signature], self._pv_client_pub + identifier + self._pv_server_pub
            )
            _LOGGER.info("Pair-verify OK for paired client %s", identifier_str)
            return True
        except Exception:
            # Fail closed on ANY failure (bad signature, ChaCha InvalidTag, malformed TLV, or M3
            # arriving without M1 so _pv_* is unset). Returning False makes _m3_verify send the
            # standard auth rejection rather than letting the exception escape unanswered.
            _LOGGER.warning("Pair-verify rejected (bad/unknown/malformed proof)")
            return False

    def _m3_verify(self, pairing_data):
        if not self._verify_client(pairing_data):
            self.send_to_client(
                FrameType.PV_Next,
                {"_pd": write_tlv({TlvValue.SeqNo: b"\x04", TlvValue.Error: bytes([ErrorCode.Authentication])})},
            )
            return
        self.send_to_client(
            FrameType.PV_Next, {"_pd": write_tlv({TlvValue.SeqNo: b"\x04"})}
        )
        self.enable_encryption(self.output_key, self.input_key)

    def _m1_setup(self, pairing_data):
        tlv = write_tlv(
            {
                TlvValue.SeqNo: b"\x02",
                TlvValue.Salt: binascii.unhexlify(self.salt),
                TlvValue.PublicKey: binascii.unhexlify(self.session.public),
                27: b"\x01",
            }
        )
        self.send_to_client(FrameType.PS_Next, {"_pd": tlv, "_pwTy": 1})

    def _m3_setup(self, pairing_data):
        pubkey = binascii.hexlify(pairing_data[TlvValue.PublicKey]).decode()
        self.session.process(pubkey, self.salt)

        if self.session.verify_proof(binascii.hexlify(pairing_data[TlvValue.Proof])):
            proof = binascii.unhexlify(self.session.key_proof_hash)
            tlv = {TlvValue.Proof: proof, TlvValue.SeqNo: b"\x04"}
        else:
            tlv = {
                TlvValue.Error: bytes([ErrorCode.Authentication]),
                TlvValue.SeqNo: b"\x04",
            }

        self.send_to_client(FrameType.PS_Next, {"_pd": write_tlv(tlv)})

    def _m5_setup(self, pairing_data):
        session_key = hkdf_expand(
            "Pair-Setup-Encrypt-Salt",
            "Pair-Setup-Encrypt-Info",
            binascii.unhexlify(self.session.key),
        )

        chacha = chacha20.Chacha20Cipher(session_key, session_key)
        try:
            client_tlv = read_tlv(
                chacha.decrypt(pairing_data[TlvValue.EncryptedData], nonce="PS-Msg05".encode())
            )
        except Exception:
            _LOGGER.warning("Pair-setup M5 could not be decrypted/parsed — rejecting")
            self._send_setup_error()
            return

        _LOGGER.debug("Received pair-setup M5 encrypted payload")

        if not self._register_setup_client(client_tlv):
            self._send_setup_error()
            return

        acc_device_x = hkdf_expand(
            "Pair-Setup-Accessory-Sign-Salt",
            "Pair-Setup-Accessory-Sign-Info",
            binascii.unhexlify(self.session.key),
        )

        other = {
            "altIRK": b"-\x54\xe0\x7a\x88*en\x11\xab\x82v-'%\xc5",
            "accountID": "DC6A7CB6-CA1A-4BF4-880D-A61B717814DB",
            "model": "AppleTV6,2",
            "wifiMAC": b"@\xff\xa1\x8f\xa1\xb9",
            "name": "Living Room",
            "mac": b"@\xc4\xff\x8f\xb1\x99",
        }

        device_info = acc_device_x + self.unique_id + self.keys.auth_pub
        signature = self.keys.sign.sign(device_info)

        tlv = {
            TlvValue.Identifier: self.unique_id,
            TlvValue.PublicKey: self.keys.auth_pub,
            TlvValue.Signature: signature,
            17: opack.pack(other),
        }

        tlv = write_tlv(tlv)

        chacha = chacha20.Chacha20Cipher(session_key, session_key)
        encrypted = chacha.encrypt(tlv, nonce="PS-Msg06".encode())

        tlv = write_tlv({TlvValue.SeqNo: b"\x06", TlvValue.EncryptedData: encrypted})

        self.send_to_client(FrameType.PS_Next, {"_pd": tlv})
        self.has_paired()

    def _register_setup_client(self, client_tlv) -> bool:
        """Validate + record the controller's long-term key from M5. Fails closed.

        Requires Identifier + PublicKey + Signature, a 32-byte Ed25519 LTPK, a UTF-8 identifier, and a
        valid controller signature over ``iOSDeviceX || identifier || LTPK`` (proving the controller
        holds the matching private key). Any failure returns False so the caller rejects the setup;
        nothing is stored.
        """
        if self._paired is None:
            return True  # ephemeral/no-store mode: nothing to persist or enforce

        if not all(t in client_tlv for t in (TlvValue.Identifier, TlvValue.PublicKey, TlvValue.Signature)):
            _LOGGER.warning("Pair-setup M5 missing identifier/public key/signature — rejecting")
            return False

        identifier = client_tlv[TlvValue.Identifier]
        ltpk = client_tlv[TlvValue.PublicKey]
        if len(ltpk) != 32:
            _LOGGER.warning("Pair-setup M5 long-term key is %d bytes (need 32) — rejecting", len(ltpk))
            return False
        try:
            identifier_str = identifier.decode("utf-8")  # strict: reject non-UTF-8 identifiers
        except UnicodeDecodeError:
            _LOGGER.warning("Pair-setup M5 identifier is not valid UTF-8 — rejecting")
            return False

        if not verify_controller_signature(
            binascii.unhexlify(self.session.key), identifier, ltpk, client_tlv[TlvValue.Signature]
        ):
            _LOGGER.warning("Pair-setup M5 signature check FAILED for %s — rejecting", identifier_str)
            return False

        self._paired.add(identifier_str, ltpk)
        return True

    def _send_setup_error(self):
        self.send_to_client(
            FrameType.PS_Next,
            {"_pd": write_tlv({TlvValue.SeqNo: b"\x06", TlvValue.Error: bytes([ErrorCode.Authentication])})},
        )

    @abstractmethod
    def send_to_client(self, frame_type: FrameType, data: object) -> None:
        """Send data to client device (iOS)."""

    @abstractmethod
    def enable_encryption(self, output_key: bytes, input_key: bytes) -> None:
        """Enable encryption with the specified keys."""

    @staticmethod
    def has_paired():
        """Call when a client has paired."""

"""Companion server-side pairing (SRP pair-setup + verify) mixin. Origin: pyatv v0.18.0 (MIT)."""

from abc import ABC, abstractmethod
import binascii
from collections import namedtuple
from collections.abc import Callable
from enum import Enum, auto
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
from .identity import PRIVATE_KEY, SERVER_IDENTIFIER
from .enums import FrameType
from .guardrails import PairSetupAttemptAdmission
from . import chacha20, opack

_LOGGER = logging.getLogger(__name__)

ServerKeys = namedtuple("ServerKeys", "sign auth_pub")


class AuthenticationPhase(Enum):
    """The only valid pre-encryption HAP exchange positions for one TCP connection."""

    IDLE = auto()
    SETUP_M3 = auto()
    SETUP_M5 = auto()
    SETUP_COMPLETE = auto()
    VERIFY_M3 = auto()
    ENCRYPTED = auto()
    FAILED = auto()
    CLOSED = auto()


def generate_keys(seed):
    signing_key = Ed25519PrivateKey.from_private_bytes(seed)
    return ServerKeys(
        sign=signing_key,
        auth_pub=signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        ),
    )


def new_server_session(pin):
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

    # Do not supply a private exponent: srptools creates a fresh 1024-bit SystemRandom value for
    # every session. The persisted Ed25519 identity proves M6 and must never become SRP state.
    session = SRPServerSession(context_server, verifier)

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
    def __init__(
        self,
        device_name,
        unique_id=SERVER_IDENTIFIER,
        private_key=PRIVATE_KEY,
        paired_clients=None,
        require_paired=False,
        pairing_window=None,
        server_identity_generation=None,
        server_session_factory: Callable[[str], tuple[object, str]] | None = None,
    ):
        self.device_name = device_name
        self.unique_id = unique_id.encode()
        self._server_identity_generation = server_identity_generation
        self.input_key = None
        self.output_key = None
        self.keys = generate_keys(private_key)
        # SRP material is intentionally deferred until a currently-open enrollment window starts
        # pair-setup. Keeping it here would leave a static/configured PIN valid forever.
        # Keep SRP setup material distinct from FakeCompanionService.session, which is the
        # per-connection TVRC/RTI state. Pair setup must not replace that protocol state.
        self._setup_session = None
        self._setup_salt = None
        self._setup_pin = None
        self._setup_window_expiry = None
        self._setup_window_generation = None
        self._setup_proof_verified = False
        # A dependency seam keeps admission tests focused on allocations rather than elapsed time.
        # Leave the default unresolved so protocol tests can patch ``new_server_session`` at call time.
        self._server_session_factory = server_session_factory
        self._pairing_window = pairing_window
        # Persist client LTPKs at setup and verify their signature at pair-verify, so only
        # enrollment-paired clients can connect. Disabled (legacy permissive) when no store is provided.
        self._paired = paired_clients
        self._require_paired = require_paired and paired_clients is not None
        self._verified_client_identifier = None
        self._verified_client_ltpk = None
        self._pv_session_key = None
        self._pv_server_pub = None
        self._pv_client_pub = None
        self._auth_phase = AuthenticationPhase.IDLE

    @property
    def authentication_phase(self) -> AuthenticationPhase:
        """Return this connection's explicit pairing/authentication phase."""
        return self._auth_phase

    @property
    def authentication_is_encrypted(self) -> bool:
        """Whether pair-verify completed and no further cleartext auth frame is valid."""
        return self._auth_phase is AuthenticationPhase.ENCRYPTED

    def reset_authentication_state(self, *, connection_closed: bool = False) -> None:
        """Erase transient pairing material when a TCP connection starts or ends."""
        self._clear_pending_auth_state()
        self._auth_phase = (
            AuthenticationPhase.CLOSED if connection_closed else AuthenticationPhase.IDLE
        )

    def handle_auth_frame(self, frame_type: FrameType, data: object) -> bool:
        """Advance exactly one valid HAP authentication transition.

        The Companion transport closes the connection when this returns ``False``.  Auth failures
        therefore never leave a second chance to reuse the preceding M1 material or an old M3.
        """
        if self.authentication_is_encrypted:
            return False

        try:
            pairing_data, seqno = self._decode_auth_frame(data)
        except Exception:
            return self._reject_auth_transition(frame_type)

        transition = self._next_auth_transition(frame_type, seqno)
        if transition is None:
            return self._reject_auth_transition(frame_type, seqno)

        handler_name, next_phase = transition
        _LOGGER.debug(
            "Received auth frame: phase=%s type=%s seqno=%s",
            self._auth_phase.name,
            frame_type.name,
            seqno,
        )
        try:
            completed = getattr(self, handler_name)(pairing_data)
        except Exception:
            _LOGGER.warning("Authentication handler failed; closing connection", exc_info=True)
            return self._reject_auth_transition(frame_type, seqno)

        if not completed:
            self._clear_pending_auth_state()
            self._auth_phase = AuthenticationPhase.FAILED
            return False

        self._auth_phase = next_phase
        return True

    @staticmethod
    def _decode_auth_frame(data: object) -> tuple[dict, int]:
        if not isinstance(data, dict):
            raise ValueError("auth OPACK payload is not a mapping")
        pairing_blob = data.get("_pd")
        if not isinstance(pairing_blob, bytes):
            raise ValueError("auth OPACK payload has no TLV bytes")
        pairing_data = read_tlv(pairing_blob)
        seqno = pairing_data.get(TlvValue.SeqNo)
        if not isinstance(seqno, bytes) or len(seqno) != 1:
            raise ValueError("auth TLV has no one-byte sequence")
        return pairing_data, seqno[0]

    def _next_auth_transition(
        self, frame_type: FrameType, seqno: int
    ) -> tuple[str, AuthenticationPhase] | None:
        phase = self._auth_phase
        if phase is AuthenticationPhase.IDLE:
            if frame_type is FrameType.PS_Start and seqno == 1:
                return "_m1_setup", AuthenticationPhase.SETUP_M3
            if frame_type is FrameType.PV_Start and seqno == 1:
                return "_m1_verify", AuthenticationPhase.VERIFY_M3
        elif phase is AuthenticationPhase.SETUP_M3:
            if frame_type is FrameType.PS_Next and seqno == 3:
                return "_m3_setup", AuthenticationPhase.SETUP_M5
        elif phase is AuthenticationPhase.SETUP_M5:
            if frame_type is FrameType.PS_Next and seqno == 5:
                return "_m5_setup", AuthenticationPhase.SETUP_COMPLETE
        elif phase is AuthenticationPhase.SETUP_COMPLETE:
            # iOS may complete the HAP enrollment exchange and then pair-verify without opening a
            # second TCP connection. Only that fresh verify M1 is valid after setup M6.
            if frame_type is FrameType.PV_Start and seqno == 1:
                return "_m1_verify", AuthenticationPhase.VERIFY_M3
        elif phase is AuthenticationPhase.VERIFY_M3:
            if frame_type is FrameType.PV_Next and seqno == 3:
                return "_m3_verify", AuthenticationPhase.ENCRYPTED
            # When the advertised accessory identity changes, iOS probes Pair-Verify first, sees the
            # replacement identity in M2, then falls back to Pair-Setup on this same TCP connection.
            if frame_type is FrameType.PS_Start and seqno == 1:
                return "_m1_setup", AuthenticationPhase.SETUP_M3
        return None

    def _reject_auth_transition(self, frame_type: FrameType, seqno: int | None = None) -> bool:
        """Respond to an invalid auth transition while erasing all reusable material."""
        self._clear_pending_auth_state()
        self._auth_phase = AuthenticationPhase.FAILED
        if frame_type in (FrameType.PS_Start, FrameType.PS_Next):
            self._reject_setup_sequence(seqno)
        else:
            self._send_verify_error()
        return False

    def _m1_verify(self, pairing_data) -> bool:
        self._clear_verify_session()
        self._clear_verified_client()
        if self._identity_reset_in_progress():
            _LOGGER.warning("Pair-verify rejected while server identity reset is pending")
            self._send_verify_error()
            return False
        # Fresh ephemeral X25519 per verify session (HAP requirement). A STATIC server ECDH key would
        # make the session key a function of only the client's ephemeral input, so a replayed M1
        # reproduces the same keys + nonce counters and lets recorded encrypted frames be replayed.
        # Our long-term Ed25519 identity key (self.keys.sign) still signs M2 to prove who we are.
        try:
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

            # The client's proof (its pairing identifier + signature) arrives encrypted in M3, not
            # here, so hold only this M1's ephemeral material until its one matching M3 arrives.
            self._pv_session_key = session_key
            self._pv_server_pub = server_pub_key
            self._pv_client_pub = client_pub_key
        except Exception:
            _LOGGER.warning("Pair-verify M1 rejected (malformed public key)")
            self._clear_verify_session()
            self._send_verify_error()
            return False

        self.send_to_client(FrameType.PV_Next, {"_pd": tlv})
        return True

    def _verify_client(self, pairing_data) -> bool:
        """Authorize an M3 pair-verify: the client must be enrollment-paired with a valid signature.

        Fails closed when ``require_paired`` is on: an empty store, an unknown client, or a bad
        signature all reject (no encryption enabled). Pair-setup records the client's long-term key
        before it ever runs verify, so a legitimate client is always present here; an empty store
        means nobody enrolled (or it was cleared/lost) — precisely when a session must NOT be
        granted. iOS responds to the rejection by falling back to window-gated pair-setup.
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
                _LOGGER.warning("Pair-verify from unknown client — rejecting")
                self._clear_verified_client()
                return False
            # HAP construction: the controller signs clientEphemeralPK || pairingID || serverEphemeralPK.
            Ed25519PublicKey.from_public_bytes(ltpk).verify(
                sub[TlvValue.Signature], self._pv_client_pub + identifier + self._pv_server_pub
            )
            self._verified_client_identifier = identifier_str
            self._verified_client_ltpk = ltpk
            _LOGGER.info("Pair-verify OK for paired client")
            return True
        except Exception:
            # Fail closed on ANY failure (bad signature, ChaCha InvalidTag, malformed TLV, or M3
            # arriving without M1 so _pv_* is unset). Returning False makes _m3_verify send the
            # standard auth rejection rather than letting the exception escape unanswered.
            _LOGGER.warning("Pair-verify rejected (bad/unknown/malformed proof)")
            self._clear_verified_client()
            return False

    def verified_client_is_authorized(self) -> bool:
        """Re-authorize the pair-verified connection against the current paired-client store.

        A file-backed store performs a metadata check and reloads only after an atomic mutation. The
        in-memory/no-path case remains coherent for protocol tests and legacy ephemeral use: it checks
        the bound key against that in-memory store rather than pretending revocation is possible.
        """
        if not getattr(self, "_require_paired", False):
            return True
        identifier = getattr(self, "_verified_client_identifier", None)
        ltpk = getattr(self, "_verified_client_ltpk", None)
        if not identifier or ltpk is None:
            return False
        try:
            return self._paired.authorizes(identifier, ltpk)
        except Exception:
            return False

    def _clear_verified_client(self) -> None:
        self._verified_client_identifier = None
        self._verified_client_ltpk = None

    def _m3_verify(self, pairing_data) -> bool:
        if not self._verify_client(pairing_data):
            self._send_verify_error()
            self._clear_verify_session()
            return False
        output_key = self.output_key
        input_key = self.input_key
        if not isinstance(output_key, bytes) or not isinstance(input_key, bytes):
            self._clear_verified_client()
            self._clear_verify_session()
            self._send_verify_error()
            return False
        self.send_to_client(
            FrameType.PV_Next, {"_pd": write_tlv({TlvValue.SeqNo: b"\x04"})}
        )
        self.enable_encryption(output_key, input_key)
        self._clear_verify_session()
        return True

    def _m1_setup(self, pairing_data) -> bool:
        # A window-gated setup may be iOS's fallback from a pending verify M2. Never let that
        # abandoned exchange's ECDH keys or claimed client identity survive into enrollment.
        self._clear_verify_session()
        self._clear_verified_client()
        admission = self.pair_setup_m1_admission()
        if not admission.allowed:
            self._clear_setup_session()
            self._send_setup_admission_error(admission)
            return False
        if self._identity_reset_in_progress():
            _LOGGER.warning("Pair-setup rejected while server identity reset is pending")
            self._clear_setup_session()
            self._send_setup_error(seqno=b"\x02")
            return False
        retry_after = self.pair_setup_backoff()
        if retry_after:
            self._clear_setup_session()
            self._send_setup_backoff(retry_after)
            return False

        window = self._active_pairing_window()
        if window is None:
            self._clear_setup_session()
            self._send_setup_error(seqno=b"\x02")
            return False
        if self._setup_session is not None:
            # A new M1 abandons any unfinished handshake. Count it before replacing its ephemeral
            # state so a malformed peer cannot allocate unbounded fresh SRP sessions by resetting M1.
            self.pairing_failed()
            self._clear_setup_session()
            retry_after = self.pair_setup_backoff()
            if retry_after:
                self._send_setup_backoff(retry_after)
                return False

        session_factory = self._server_session_factory or new_server_session
        self._setup_session, self._setup_salt = session_factory(window.pin)
        self._setup_pin = window.pin
        self._setup_window_expiry = getattr(window, "expires_at", None)
        self._setup_window_generation = getattr(window, "generation", None)
        self._setup_proof_verified = False
        tlv = write_tlv(
            {
                TlvValue.SeqNo: b"\x02",
                TlvValue.Salt: binascii.unhexlify(self._setup_salt),
                TlvValue.PublicKey: binascii.unhexlify(self._setup_session.public),
                27: b"\x01",
            }
        )
        self.send_to_client(FrameType.PS_Next, {"_pd": tlv, "_pwTy": 1})
        return True

    def _m3_setup(self, pairing_data) -> bool:
        setup_session = self._setup_session
        if (
            setup_session is None
            or self._setup_salt is None
            or getattr(self, "_setup_proof_verified", False)
        ):
            self.pairing_failed()
            self._clear_setup_session()
            self._send_setup_m4_error()
            return False
        if not self._setup_window_is_current():
            self._clear_setup_session()
            self._send_setup_error(seqno=b"\x04")
            return False
        try:
            pubkey = binascii.hexlify(pairing_data[TlvValue.PublicKey]).decode()
            setup_session.process(pubkey, self._setup_salt)
            verified = setup_session.verify_proof(
                binascii.hexlify(pairing_data[TlvValue.Proof])
            )
        except Exception:
            self.pairing_failed()
            self._clear_setup_session()
            self._send_setup_m4_error()
            return False

        if verified:
            self._setup_proof_verified = True
            proof = binascii.unhexlify(setup_session.key_proof_hash)
            tlv = {TlvValue.Proof: proof, TlvValue.SeqNo: b"\x04"}
        else:
            self.pairing_failed()
            self._clear_setup_session()
            tlv = {
                TlvValue.Error: bytes([ErrorCode.Authentication]),
                TlvValue.SeqNo: b"\x04",
            }

        self.send_to_client(FrameType.PS_Next, {"_pd": write_tlv(tlv)})
        return bool(verified)

    def _m5_setup(self, pairing_data) -> bool:
        setup_session = self._setup_session
        if setup_session is None or not getattr(self, "_setup_proof_verified", False):
            self.pairing_failed()
            self._clear_setup_session()
            self._send_setup_error()
            return False
        if not self._setup_window_is_current():
            self._clear_setup_session()
            self._send_setup_error()
            return False
        session_key = hkdf_expand(
            "Pair-Setup-Encrypt-Salt",
            "Pair-Setup-Encrypt-Info",
            binascii.unhexlify(setup_session.key),
        )

        chacha = chacha20.Chacha20Cipher(session_key, session_key)
        try:
            client_tlv = read_tlv(
                chacha.decrypt(pairing_data[TlvValue.EncryptedData], nonce="PS-Msg05".encode())
            )
        except Exception:
            _LOGGER.warning("Pair-setup M5 could not be decrypted/parsed — rejecting")
            self.pairing_failed()
            self._clear_setup_session()
            self._send_setup_error()
            return False

        _LOGGER.debug("Received pair-setup M5 encrypted payload")

        error = self._register_setup_client(
            client_tlv, setup_session_key=binascii.unhexlify(setup_session.key)
        )
        if error is not None:
            if error is ErrorCode.Authentication:
                self.pairing_failed()
            self._clear_setup_session()
            self._send_setup_error(error=error)
            return False

        acc_device_x = hkdf_expand(
            "Pair-Setup-Accessory-Sign-Salt",
            "Pair-Setup-Accessory-Sign-Info",
            binascii.unhexlify(setup_session.key),
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
        self._clear_setup_session()
        return True

    def _register_setup_client(self, client_tlv, *, setup_session_key: bytes):
        """Validate + record the controller's long-term key from M5. Fails closed.

        Requires Identifier + PublicKey + Signature, a 32-byte Ed25519 LTPK, a UTF-8 identifier, and a
        valid controller signature over ``iOSDeviceX || identifier || LTPK`` (proving the controller
        holds the matching private key). The returned error code lets a full store report HAP's
        ``MaxPeers`` rather than an indistinguishable authentication failure.
        """
        if self._paired is None:
            return None  # ephemeral/no-store mode: nothing to persist or enforce

        if not all(t in client_tlv for t in (TlvValue.Identifier, TlvValue.PublicKey, TlvValue.Signature)):
            _LOGGER.warning("Pair-setup M5 missing identifier/public key/signature — rejecting")
            return ErrorCode.Authentication

        identifier = client_tlv[TlvValue.Identifier]
        ltpk = client_tlv[TlvValue.PublicKey]
        if len(ltpk) != 32:
            _LOGGER.warning("Pair-setup M5 long-term key is %d bytes (need 32) — rejecting", len(ltpk))
            return ErrorCode.Authentication
        try:
            identifier_str = identifier.decode("utf-8")  # strict: reject non-UTF-8 identifiers
        except UnicodeDecodeError:
            _LOGGER.warning("Pair-setup M5 identifier is not valid UTF-8 — rejecting")
            return ErrorCode.Authentication

        if not verify_controller_signature(
            setup_session_key, identifier, ltpk, client_tlv[TlvValue.Signature]
        ):
            _LOGGER.warning("Pair-setup M5 signature check failed — rejecting")
            return ErrorCode.Authentication

        try:
            transactional_commit = getattr(self._pairing_window, "mutate_if_current", None)
            if transactional_commit is not None:
                generation = getattr(self, "_setup_window_generation", None)
                if not isinstance(generation, str):
                    _LOGGER.warning("Pair-setup M5 has no enrollment-window generation — rejecting")
                    return ErrorCode.Authentication
                binding = self._server_identity_binding()
                if binding is None:
                    _LOGGER.warning("Pair-setup M5 has no running server identity binding — rejecting")
                    return ErrorCode.Authentication
                current, _ = transactional_commit(
                    generation,
                    lambda: self._paired.add_locked(identifier_str, ltpk),
                    server_identifier=binding[0],
                    server_generation=binding[1],
                )
                if not current:
                    _LOGGER.warning(
                        "Pair-setup M5 enrollment window changed, expired, or names another server — "
                        "rejecting"
                    )
                    return ErrorCode.Authentication
            else:
                # Lightweight direct protocol fixtures have no persistent transaction store. Production
                # always uses PairingWindowStore above, whose callback holds the shared state lock.
                if not self._setup_window_is_current():
                    _LOGGER.warning("Pair-setup M5 enrollment window changed or expired — rejecting")
                    return ErrorCode.Authentication
                self._paired.add(identifier_str, ltpk)
        except Exception as exc:
            from .paired_clients import PairedClientsFullError

            if isinstance(exc, PairedClientsFullError):
                _LOGGER.warning("Pair-setup rejected: paired-client capacity reached")
                return ErrorCode.MaxPeers
            _LOGGER.warning("Pair-setup could not persist the paired client — rejecting")
            return ErrorCode.Authentication
        return None

    def _setup_window_is_current(self) -> bool:
        """Keep a handshake bound to the same unexpired window that started it."""
        if self._pairing_window is None:
            return True  # direct protocol tests and intentionally ephemeral base-server use
        window = self._active_pairing_window()
        current = (
            window is not None
            and window.pin == self._setup_pin
            and getattr(window, "expires_at", None) == self._setup_window_expiry
        )
        if not current:
            return False
        generation = getattr(self, "_setup_window_generation", None)
        return generation is None or getattr(window, "generation", None) == generation

    def _active_pairing_window(self):
        """Read a persistent window under its transaction lock when identity binding is available."""
        if self._pairing_window is None:
            return None
        active_for_server = getattr(self._pairing_window, "active_for_server", None)
        if active_for_server is None:
            # Lightweight protocol fixtures deliberately model only the old `active` contract.
            return self._pairing_window.active()
        binding = self._server_identity_binding()
        if binding is None:
            return None
        return active_for_server(*binding)

    def _identity_reset_in_progress(self) -> bool:
        """Fail closed while either durable pairing-state recovery checkpoint remains."""
        paired = getattr(self, "_paired", None)
        paired_check = getattr(paired, "reset_in_progress", None)
        if paired_check is not None:
            try:
                if paired_check():
                    return True
            except Exception:
                return True
        pairing_window = getattr(self, "_pairing_window", None)
        state_dir = getattr(pairing_window, "state_dir", None)
        if state_dir is None:
            return False
        try:
            from .identity_reset import pairing_reset_in_progress

            return pairing_reset_in_progress(state_dir)
        except Exception:
            return True

    def _server_identity_binding(self) -> tuple[str, str] | None:
        generation = getattr(self, "_server_identity_generation", None)
        if not isinstance(generation, str):
            return None
        try:
            identifier = self.unique_id.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            return None
        return identifier, generation

    def _clear_verify_session(self) -> None:
        """Discard the one-use ECDH material and derived transport keys for pair-verify."""
        self.input_key = None
        self.output_key = None
        self._pv_session_key = None
        self._pv_server_pub = None
        self._pv_client_pub = None

    def _clear_pending_auth_state(self) -> None:
        self._clear_setup_session()
        self._clear_verify_session()
        self._clear_verified_client()

    def _clear_setup_session(self) -> None:
        self._setup_session = None
        self._setup_salt = None
        self._setup_pin = None
        self._setup_window_expiry = None
        self._setup_window_generation = None
        self._setup_proof_verified = False

    def _reject_setup_sequence(self, seqno: int | None = None) -> None:
        """Fail a malformed pair-setup transition without retaining reusable SRP state."""
        self.pairing_failed()
        self._clear_setup_session()
        response_seqno = b"\x02" if seqno is None or seqno <= 1 else b"\x04" if seqno <= 3 else b"\x06"
        self._send_setup_error(seqno=response_seqno)

    def _send_verify_error(self) -> None:
        self.send_to_client(
            FrameType.PV_Next,
            {"_pd": write_tlv({TlvValue.SeqNo: b"\x04", TlvValue.Error: bytes([ErrorCode.Authentication])})},
        )

    def _send_setup_error(self, *, seqno: bytes = b"\x06", error: ErrorCode = ErrorCode.Authentication):
        self.send_to_client(
            FrameType.PS_Next,
            {"_pd": write_tlv({TlvValue.SeqNo: seqno, TlvValue.Error: bytes([error])})},
        )

    def _send_setup_m4_error(self):
        self.send_to_client(
            FrameType.PS_Next,
            {"_pd": write_tlv({TlvValue.SeqNo: b"\x04", TlvValue.Error: bytes([ErrorCode.Authentication])})},
        )

    def _send_setup_backoff(self, retry_after: int):
        self.send_to_client(
            FrameType.PS_Next,
            {
                "_pd": write_tlv(
                    {
                        TlvValue.SeqNo: b"\x02",
                        TlvValue.Error: bytes([ErrorCode.BackOff]),
                        TlvValue.BackOff: int(retry_after).to_bytes(2, byteorder="little"),
                    }
                )
            },
        )

    def _send_setup_admission_error(self, admission: PairSetupAttemptAdmission) -> None:
        """Return the HAP error selected by shared M1 admission without creating SRP state."""
        error = admission.error
        if error is ErrorCode.BackOff:
            self._send_setup_backoff(admission.retry_after or 1)
            return
        self._send_setup_error(
            seqno=b"\x02",
            error=error if error is not None else ErrorCode.Busy,
        )

    def pair_setup_m1_admission(self) -> PairSetupAttemptAdmission:
        """Atomically consume a setup M1 start when the concrete service applies a shared limit."""
        return PairSetupAttemptAdmission(True)

    def pair_setup_backoff(self) -> int:
        """Return a retry delay before pair-setup M1, or zero when it may proceed."""
        return 0

    def pairing_failed(self) -> None:
        """Record a failed pair setup, if the concrete service applies throttling."""

    @abstractmethod
    def send_to_client(self, frame_type: FrameType, data: object) -> None:
        """Send data to client device (iOS)."""

    @abstractmethod
    def enable_encryption(self, output_key: bytes, input_key: bytes) -> None:
        """Enable encryption with the specified keys."""

    @staticmethod
    def has_paired():
        """Call when a client has paired."""

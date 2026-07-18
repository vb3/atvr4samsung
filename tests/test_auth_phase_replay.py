"""State-machine regressions for cleartext Companion authentication frames."""
from __future__ import annotations

import asyncio
import binascii
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
import pytest
from srptools import SRPClientSession, SRPContext, constants

from atvr4samsung.companion.protocol import chacha20, opack
from atvr4samsung.companion.protocol.appletv import FakeCompanionService, FakeCompanionState
from atvr4samsung.companion.protocol.auth import AuthenticationPhase
from atvr4samsung.companion.protocol.enums import FrameType
from atvr4samsung.companion.protocol.paired_clients import PairedClients
from atvr4samsung.companion.protocol.support import hkdf_expand
from atvr4samsung.companion.protocol.tlv8 import ErrorCode, TlvValue, read_tlv, write_tlv

_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_PAIRING_PIN = "5718"


class _Transport:
    def __init__(self) -> None:
        self.closed = False
        self.writes: list[bytes] = []

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def get_extra_info(self, name: str):
        return ("192.0.2.10", 49152) if name == "peername" else None


class _Window:
    pin = _PAIRING_PIN
    expires_at = 600.0


class _WindowStore:
    @staticmethod
    def active() -> _Window:
        return _Window()


def _frame(frame_type: FrameType, payload: bytes) -> bytes:
    return bytes([frame_type.value]) + len(payload).to_bytes(3, "big") + payload


def _auth_wire(frame_type: FrameType, tlv: dict) -> bytes:
    return _frame(frame_type, opack.pack({"_pd": write_tlv(tlv)}))


def _service(*, paired: PairedClients | None = None, pairing_window=None):
    global _TEST_LOOP
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if _TEST_LOOP is None or _TEST_LOOP.is_closed():
            _TEST_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_TEST_LOOP)

    service = FakeCompanionService(
        FakeCompanionState(),
        paired_clients=paired,
        require_paired=paired is not None,
        pairing_window=pairing_window,
        authentication_timeout=0,
    )
    transport = _Transport()
    service.connection_made(transport)
    return service, transport


def _last_auth_response(transport: _Transport) -> tuple[FrameType, dict]:
    wire = transport.writes[-1]
    payload_length = int.from_bytes(wire[1:4], "big")
    assert len(wire) == 4 + payload_length
    message, _ = opack.unpack(wire[4:])
    return FrameType(wire[0]), read_tlv(message["_pd"])


def _raw_ltpk(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _start_pair_verify(
    service: FakeCompanionService,
    transport: _Transport,
    controller_key: Ed25519PrivateKey,
    identifier: bytes,
) -> tuple[bytes, bytes, bytes]:
    controller_ephemeral = X25519PrivateKey.generate()
    controller_public = controller_ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    m1 = _auth_wire(
        FrameType.PV_Start,
        {TlvValue.SeqNo: b"\x01", TlvValue.PublicKey: controller_public},
    )
    service.data_received(m1)

    frame_type, m2 = _last_auth_response(transport)
    assert frame_type is FrameType.PV_Next
    assert m2[TlvValue.SeqNo] == b"\x02"
    server_public = m2[TlvValue.PublicKey]
    shared_key = controller_ephemeral.exchange(X25519PublicKey.from_public_bytes(server_public))
    return controller_public, server_public, shared_key


def _verify_m3(
    controller_key: Ed25519PrivateKey,
    identifier: bytes,
    controller_public: bytes,
    server_public: bytes,
    shared_key: bytes,
) -> bytes:
    session_key = hkdf_expand(
        "Pair-Verify-Encrypt-Salt", "Pair-Verify-Encrypt-Info", shared_key
    )
    signature = controller_key.sign(controller_public + identifier + server_public)
    plaintext = write_tlv(
        {TlvValue.Identifier: identifier, TlvValue.Signature: signature}
    )
    encrypted = chacha20.Chacha20Cipher(session_key, session_key).encrypt(
        plaintext, nonce=b"PV-Msg03"
    )
    return _auth_wire(
        FrameType.PV_Next,
        {TlvValue.SeqNo: b"\x03", TlvValue.EncryptedData: encrypted},
    )


def _establish_pair_verify() -> tuple[FakeCompanionService, _Transport, bytes, bytes]:
    identifier = b"phone-a"
    controller_key = Ed25519PrivateKey.generate()
    paired = PairedClients(None)
    paired.add(identifier.decode(), _raw_ltpk(controller_key))
    service, transport = _service(paired=paired)
    controller_public, server_public, shared_key = _start_pair_verify(
        service, transport, controller_key, identifier
    )
    m3 = _verify_m3(
        controller_key, identifier, controller_public, server_public, shared_key
    )
    service.data_received(m3)
    assert service.authentication_phase is AuthenticationPhase.ENCRYPTED
    assert service.chacha is not None
    return service, transport, shared_key, m3


def _encrypted_command(shared_key: bytes, message: dict) -> bytes:
    plaintext = opack.pack(message)
    header = bytes([FrameType.E_OPACK.value]) + (len(plaintext) + 16).to_bytes(3, "big")
    client_key = hkdf_expand("", "ClientEncrypt-main", shared_key)
    payload = chacha20.Chacha20Cipher(
        client_key, b"\x00" * 32, nonce_length=12
    ).encrypt(plaintext, aad=header)
    return header + payload


def test_pair_setup_then_pair_verify_uses_the_only_compatible_transition():
    """A completed enrollment can authenticate on the same TCP connection, as iOS does."""
    controller_key = Ed25519PrivateKey.generate()
    identifier = b"phone-a"
    paired = PairedClients(None)
    service, transport = _service(paired=paired, pairing_window=_WindowStore())

    service.data_received(_auth_wire(FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"}))
    assert service.authentication_phase is AuthenticationPhase.SETUP_M3
    _, m2 = _last_auth_response(transport)

    client_context = SRPContext(
        "Pair-Setup",
        _PAIRING_PIN,
        prime=constants.PRIME_3072,
        generator=constants.PRIME_3072_GEN,
        hash_func=hashlib.sha512,
        bits_salt=128,
    )
    client = SRPClientSession(client_context)
    client.process(
        binascii.hexlify(m2[TlvValue.PublicKey]).decode(),
        binascii.hexlify(m2[TlvValue.Salt]).decode(),
    )
    service.data_received(
        _auth_wire(
            FrameType.PS_Next,
            {
                TlvValue.SeqNo: b"\x03",
                TlvValue.PublicKey: bytes.fromhex(client.public),
                TlvValue.Proof: binascii.unhexlify(client.key_proof),
            },
        )
    )
    assert service.authentication_phase is AuthenticationPhase.SETUP_M5
    _, m4 = _last_auth_response(transport)
    assert client.verify_proof(binascii.hexlify(m4[TlvValue.Proof]))

    session_key = binascii.unhexlify(client.key)
    ltpk = _raw_ltpk(controller_key)
    controller_x = hkdf_expand(
        "Pair-Setup-Controller-Sign-Salt",
        "Pair-Setup-Controller-Sign-Info",
        session_key,
    )
    signature = controller_key.sign(controller_x + identifier + ltpk)
    setup_encryption_key = hkdf_expand(
        "Pair-Setup-Encrypt-Salt",
        "Pair-Setup-Encrypt-Info",
        session_key,
    )
    encrypted_m5 = chacha20.Chacha20Cipher(
        setup_encryption_key, setup_encryption_key
    ).encrypt(
        write_tlv(
            {
                TlvValue.Identifier: identifier,
                TlvValue.PublicKey: ltpk,
                TlvValue.Signature: signature,
            }
        ),
        nonce=b"PS-Msg05",
    )
    service.data_received(
        _auth_wire(
            FrameType.PS_Next,
            {TlvValue.SeqNo: b"\x05", TlvValue.EncryptedData: encrypted_m5},
        )
    )
    assert service.authentication_phase is AuthenticationPhase.SETUP_COMPLETE
    _, m6 = _last_auth_response(transport)
    assert m6[TlvValue.SeqNo] == b"\x06"
    assert paired.authorizes(identifier.decode(), ltpk)

    controller_public, server_public, shared_key = _start_pair_verify(
        service, transport, controller_key, identifier
    )
    assert service.authentication_phase is AuthenticationPhase.VERIFY_M3
    service.data_received(
        _verify_m3(
            controller_key, identifier, controller_public, server_public, shared_key
        )
    )

    assert not transport.closed
    assert service.authentication_phase is AuthenticationPhase.ENCRYPTED
    assert service.chacha is not None


def test_pair_verify_can_fall_back_to_window_gated_pair_setup():
    controller_key = Ed25519PrivateKey.generate()
    service, transport = _service(
        paired=PairedClients(None), pairing_window=_WindowStore()
    )
    _start_pair_verify(service, transport, controller_key, b"stale-phone")
    assert service.authentication_phase is AuthenticationPhase.VERIFY_M3
    assert service.output_key is not None

    service.data_received(
        _auth_wire(FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"})
    )

    frame_type, setup_m2 = _last_auth_response(transport)
    assert not transport.closed
    assert service.authentication_phase is AuthenticationPhase.SETUP_M3
    assert frame_type is FrameType.PS_Next
    assert setup_m2[TlvValue.SeqNo] == b"\x02"
    assert TlvValue.PublicKey in setup_m2
    assert TlvValue.Salt in setup_m2
    assert service.input_key is None
    assert service.output_key is None
    assert service._pv_session_key is None
    assert service._pv_server_pub is None
    assert service._pv_client_pub is None


def test_pair_verify_fallback_is_rejected_without_an_enrollment_window():
    controller_key = Ed25519PrivateKey.generate()
    service, transport = _service(paired=PairedClients(None))
    _start_pair_verify(service, transport, controller_key, b"stale-phone")

    service.data_received(
        _auth_wire(FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"})
    )

    frame_type, error = _last_auth_response(transport)
    assert transport.closed
    assert service.authentication_phase is AuthenticationPhase.FAILED
    assert frame_type is FrameType.PS_Next
    assert error[TlvValue.SeqNo] == b"\x02"
    assert error[TlvValue.Error] == bytes([ErrorCode.Authentication])
    assert service.input_key is None
    assert service.output_key is None


def test_duplicate_pair_verify_m1_closes_without_replacing_its_pending_exchange():
    identifier = b"phone-a"
    controller_key = Ed25519PrivateKey.generate()
    paired = PairedClients(None)
    paired.add(identifier.decode(), _raw_ltpk(controller_key))
    service, transport = _service(paired=paired)

    controller_ephemeral = X25519PrivateKey.generate()
    controller_public = controller_ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    m1 = _auth_wire(
        FrameType.PV_Start,
        {TlvValue.SeqNo: b"\x01", TlvValue.PublicKey: controller_public},
    )
    service.data_received(m1)
    service.data_received(m1)

    frame_type, error = _last_auth_response(transport)
    assert transport.closed
    assert service.chacha is None
    assert service.authentication_phase is AuthenticationPhase.FAILED
    assert frame_type is FrameType.PV_Next
    assert error[TlvValue.SeqNo] == b"\x04"
    assert error[TlvValue.Error] == bytes([ErrorCode.Authentication])


def test_duplicate_pair_setup_m1_closes_without_replacing_its_srp_exchange():
    service, transport = _service(
        paired=PairedClients(None), pairing_window=_WindowStore()
    )
    m1 = _auth_wire(FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"})

    service.data_received(m1)
    service.data_received(m1)

    frame_type, error = _last_auth_response(transport)
    assert transport.closed
    assert service.authentication_phase is AuthenticationPhase.FAILED
    assert frame_type is FrameType.PS_Next
    assert error[TlvValue.SeqNo] == b"\x02"
    assert error[TlvValue.Error] == bytes([ErrorCode.Authentication])


def test_pair_verify_m3_without_its_m1_is_rejected_and_closed():
    service, transport = _service(paired=PairedClients(None))

    service.data_received(
        _auth_wire(
            FrameType.PV_Next,
            {TlvValue.SeqNo: b"\x03", TlvValue.EncryptedData: b"unmatched"},
        )
    )

    frame_type, error = _last_auth_response(transport)
    assert transport.closed
    assert service.chacha is None
    assert service.authentication_phase is AuthenticationPhase.FAILED
    assert frame_type is FrameType.PV_Next
    assert error[TlvValue.SeqNo] == b"\x04"
    assert error[TlvValue.Error] == bytes([ErrorCode.Authentication])


def test_connection_teardown_discards_a_pending_verify_m3():
    identifier = b"phone-a"
    controller_key = Ed25519PrivateKey.generate()
    paired = PairedClients(None)
    paired.add(identifier.decode(), _raw_ltpk(controller_key))
    service, transport = _service(paired=paired)
    controller_public, server_public, shared_key = _start_pair_verify(
        service, transport, controller_key, identifier
    )
    stale_m3 = _verify_m3(
        controller_key, identifier, controller_public, server_public, shared_key
    )

    service.connection_lost(None)
    replacement_transport = _Transport()
    service.connection_made(replacement_transport)
    service.data_received(stale_m3)

    assert replacement_transport.closed
    assert service.chacha is None
    assert service.authentication_phase is AuthenticationPhase.FAILED


def test_cross_type_and_malformed_auth_frames_fail_closed():
    identifier = b"phone-a"
    controller_key = Ed25519PrivateKey.generate()
    paired = PairedClients(None)
    paired.add(identifier.decode(), _raw_ltpk(controller_key))
    service, transport = _service(paired=paired)
    _start_pair_verify(service, transport, controller_key, identifier)

    service.data_received(_auth_wire(FrameType.PS_Next, {TlvValue.SeqNo: b"\x03"}))

    frame_type, error = _last_auth_response(transport)
    assert transport.closed
    assert service.authentication_phase is AuthenticationPhase.FAILED
    assert frame_type is FrameType.PS_Next
    assert error[TlvValue.SeqNo] == b"\x04"
    assert error[TlvValue.Error] == bytes([ErrorCode.Authentication])

    malformed, malformed_transport = _service(paired=PairedClients(None))
    malformed.data_received(_frame(FrameType.PV_Start, opack.pack({"_pd": b"\x06"})))
    frame_type, error = _last_auth_response(malformed_transport)
    assert malformed_transport.closed
    assert malformed.authentication_phase is AuthenticationPhase.FAILED
    assert frame_type is FrameType.PV_Next
    assert error[TlvValue.Error] == bytes([ErrorCode.Authentication])


@pytest.mark.parametrize(
    ("frame_type", "tlv"),
    [
        (FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"}),
        (FrameType.PS_Next, {TlvValue.SeqNo: b"\x03"}),
        (FrameType.PV_Start, {TlvValue.SeqNo: b"\x01"}),
        (FrameType.PV_Next, {TlvValue.SeqNo: b"\x03"}),
    ],
)
def test_every_auth_frame_after_encryption_closes_before_auth_parsing(
    frame_type: FrameType, tlv: dict
):
    service, transport, _, _ = _establish_pair_verify()
    cipher = service.chacha
    assert cipher is not None
    in_nonce = cipher.in_nonce
    out_nonce = cipher.out_nonce
    writes = len(transport.writes)

    service.data_received(_auth_wire(frame_type, tlv))

    assert transport.closed
    assert service.chacha is cipher
    assert cipher.in_nonce == in_nonce
    assert cipher.out_nonce == out_nonce
    assert len(transport.writes) == writes


def test_replayed_verify_m3_cannot_reset_aead_or_replay_a_captured_command():
    service, transport, shared_key, captured_m3 = _establish_pair_verify()
    cipher = service.chacha
    assert cipher is not None

    # A captured command at client nonce zero would execute if replayed M3 reset the server cipher.
    captured_command = _encrypted_command(
        shared_key,
        {
            "_i": "_sessionStart",
            "_x": 7,
            "_t": 2,
            "_c": {"_sid": 4242, "_srvT": "TVRemote"},
        },
    )
    service.send_event("ReplayCounter", 1, {})
    in_nonce = cipher.in_nonce
    out_nonce = cipher.out_nonce
    assert out_nonce != b"\x00" * len(out_nonce)

    service.data_received(captured_m3)

    assert transport.closed
    assert service.chacha is cipher
    assert service.authentication_phase is AuthenticationPhase.ENCRYPTED
    assert cipher.in_nonce == in_nonce
    assert cipher.out_nonce == out_nonce

    service.data_received(captured_command)

    assert service.session.sid == 0
    assert cipher.in_nonce == in_nonce
    assert cipher.out_nonce == out_nonce

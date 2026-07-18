"""Focused regressions for Companion TCP admission, framing, and redacted diagnostics."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from atvr4samsung.companion.protocol import chacha20, opack
from atvr4samsung.companion.protocol.appletv import FakeCompanionService, FakeCompanionState
from atvr4samsung.companion.protocol.auth import CompanionServerAuth
from atvr4samsung.companion.protocol.enums import FrameType
from atvr4samsung.companion.protocol.framing import FrameParser, FrameTooLarge
from atvr4samsung.companion.protocol.guardrails import (
    ConnectionAdmission,
    PairFailureLimiter,
    PairSetupAttemptLimiter,
)
from atvr4samsung.companion.protocol.tlv8 import ErrorCode, TlvValue, read_tlv, write_tlv
from atvr4samsung.companion.protocol.support import hkdf_expand
from atvr4samsung.companion.server import BridgeCompanionService

_TEST_LOOP: asyncio.AbstractEventLoop | None = None


def _frame(frame_type: FrameType | int, payload: bytes) -> bytes:
    type_code = frame_type.value if isinstance(frame_type, FrameType) else frame_type
    return bytes([type_code]) + len(payload).to_bytes(3, byteorder="big") + payload


def _auth_frame(frame_type: FrameType, tlv: dict) -> bytes:
    return _frame(frame_type, opack.pack({"_pd": write_tlv(tlv)}))


class _Transport:
    def __init__(self, source: str = "192.0.2.10") -> None:
        self.closed = False
        self.writes: list[bytes] = []
        self.source = source

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def get_extra_info(self, name: str):
        return (self.source, 49152) if name == "peername" else None


class _PassthroughCipher:
    def decrypt(self, data: bytes, *, aad: bytes) -> bytes:
        return data

    def encrypt(self, data: bytes, *, aad: bytes) -> bytes:
        return data


class _CountingSessionFactory:
    """Allocation seam: tests count attempted SRP creation without relying on elapsed time."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, pin: str) -> tuple[object, str]:
        self.calls += 1
        return type("Session", (), {"public": "01"})(), "00"


class _CompletingSessionFactory(_CountingSessionFactory):
    def __call__(self, pin: str) -> tuple[object, str]:
        self.calls += 1

        class _Session:
            public = "01"
            key_proof_hash = "00"
            key = "00" * 32

            @staticmethod
            def process(public: str, salt: str) -> None:
                pass

            @staticmethod
            def verify_proof(proof: str) -> bool:
                return True

        return _Session(), "00"


class _OpenWindow:
    @staticmethod
    def active():
        return type("Window", (), {"pin": "5718", "expires_at": 600.0})()


def _service(
    *,
    admission=None,
    timeout=0.0,
    source: str = "192.0.2.10",
    pairing_window=None,
    pair_setup_attempt_limiter=None,
    pair_failure_limiter=None,
    server_session_factory=None,
) -> tuple[FakeCompanionService, _Transport]:
    global _TEST_LOOP
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if _TEST_LOOP is None or _TEST_LOOP.is_closed():
            _TEST_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_TEST_LOOP)
    service = FakeCompanionService(
        FakeCompanionState(),
        admission=admission,
        authentication_timeout=timeout,
        pairing_window=pairing_window,
        pair_setup_attempt_limiter=pair_setup_attempt_limiter,
        pair_failure_limiter=pair_failure_limiter,
        server_session_factory=server_session_factory,
    )
    transport = _Transport(source)
    service.connection_made(transport)
    return service, transport


def _last_setup_response(transport: _Transport) -> dict:
    message, _ = opack.unpack(transport.writes[-1][4:])
    return read_tlv(message["_pd"])


def test_fragmented_frames_are_reassembled_without_retaining_extra_data():
    parser = FrameParser(max_payload=32)
    wire = _frame(FrameType.PS_Start, b"abc") + _frame(FrameType.PV_Start, b"z")

    assert list(parser.feed(wire[:2])) == []
    assert parser.buffered_bytes == 2
    frames = list(parser.feed(wire[2:7]))
    assert [(frame.type_code, frame.payload) for frame in frames] == [(FrameType.PS_Start.value, b"abc")]
    assert parser.buffered_bytes == 0
    frames = list(parser.feed(wire[7:]))
    assert [(frame.type_code, frame.payload) for frame in frames] == [(FrameType.PV_Start.value, b"z")]
    assert parser.buffered_bytes == 0


def test_oversize_declaration_is_rejected_before_payload_is_buffered():
    parser = FrameParser(max_payload=16)
    oversized_header = bytes([FrameType.PS_Start.value]) + (17).to_bytes(3, byteorder="big")

    with pytest.raises(FrameTooLarge):
        list(parser.feed(oversized_header + b"x" * 17))
    assert parser.buffered_bytes == 0


def test_partial_payload_is_bounded_until_the_complete_frame_arrives():
    parser = FrameParser(max_payload=32)
    wire = _frame(FrameType.PS_Start, b"abcdefgh")
    assert list(parser.feed(wire[:9])) == []
    assert parser.buffered_bytes == 9
    assert [frame.payload for frame in parser.feed(wire[9:])] == [b"abcdefgh"]
    assert parser.buffered_bytes == 0


def test_bridge_and_base_share_the_same_guarded_frame_loop():
    assert BridgeCompanionService.data_received is FakeCompanionService.data_received


def test_oversized_wire_frame_closes_the_connection():
    service, transport = _service()
    oversized = bytes([FrameType.PS_Start.value]) + (1_048_593).to_bytes(3, byteorder="big")
    service.data_received(oversized)
    assert transport.closed
    assert service._frame_parser.buffered_bytes == 0


def test_malformed_opack_is_tolerated_only_for_the_small_compatibility_budget():
    service, transport = _service()
    service.chacha = _PassthroughCipher()
    malformed = _frame(FrameType.E_OPACK, b"\xff")

    service.data_received(malformed)
    service.data_received(malformed)
    assert not transport.closed
    service.data_received(malformed)
    assert transport.closed


def test_pre_auth_idle_connection_expires():
    async def exercise() -> None:
        service, transport = _service(timeout=0.01)
        await asyncio.sleep(0.03)
        assert transport.closed
        service.connection_lost(None)

    asyncio.run(exercise())


def test_connection_limits_auth_transition_and_idempotent_release():
    defaults = ConnectionAdmission()
    assert (defaults.max_connections, defaults.max_unauthenticated) == (16, 8)
    admission = ConnectionAdmission(max_connections=3, max_unauthenticated=2)
    first, first_transport = _service(admission=admission)
    second, second_transport = _service(admission=admission)
    rejected, rejected_transport = _service(admission=admission)

    assert admission.connection_count == 2
    assert admission.unauthenticated_count == 2
    assert not first_transport.closed
    assert not second_transport.closed
    assert rejected_transport.closed

    first.enable_encryption(b"a" * 32, b"b" * 32)
    third, third_transport = _service(admission=admission)
    assert not third_transport.closed
    assert admission.connection_count == 3
    assert admission.unauthenticated_count == 2

    first.connection_lost(None)
    first.connection_lost(None)
    assert admission.connection_count == 2
    assert admission.unauthenticated_count == 2
    second.connection_lost(None)
    third.connection_lost(None)
    rejected.connection_lost(None)


def test_failed_pair_limiter_enforces_source_global_window_and_bounded_retention():
    now = [0.0]
    limiter = PairFailureLimiter(
        per_source_limit=5,
        global_limit=20,
        window_seconds=60,
        max_sources=2,
        clock=lambda: now[0],
    )
    for _ in range(5):
        limiter.record_failure("192.0.2.1")
    decision = limiter.check("192.0.2.1")
    assert not decision.allowed
    assert decision.retry_after == 60

    now[0] = 60.0
    assert limiter.check("192.0.2.1").allowed
    for source in ("192.0.2.2", "192.0.2.3", "192.0.2.4"):
        limiter.record_failure(source)
    assert limiter.tracked_sources == 2

    global_limiter = PairFailureLimiter(clock=lambda: now[0])
    for index in range(20):
        global_limiter.record_failure(f"198.51.100.{index}")
    assert not global_limiter.check("203.0.113.1").allowed


def test_abandoned_m1s_exhaust_shared_source_budget_before_srp_and_expire():
    now = [0.0]
    attempts = PairSetupAttemptLimiter(
        per_source_limit=2,
        global_limit=20,
        window_seconds=60,
        clock=lambda: now[0],
    )
    sessions = _CountingSessionFactory()

    for _ in range(2):
        service, transport = _service(
            source="198.51.100.8",
            pairing_window=_OpenWindow(),
            pair_setup_attempt_limiter=attempts,
            server_session_factory=sessions,
        )
        service.data_received(_auth_frame(FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"}))
        assert not transport.closed
        # Clearing connection state and disconnecting must not refund the process-wide M1 start.
        service.reset_authentication_state()
        service.connection_lost(None)

    rejected, rejected_transport = _service(
        source="198.51.100.8",
        pairing_window=_OpenWindow(),
        pair_setup_attempt_limiter=attempts,
        server_session_factory=sessions,
    )
    rejected.data_received(_auth_frame(FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"}))
    assert rejected_transport.closed
    assert _last_setup_response(rejected_transport)[TlvValue.Error] == bytes([ErrorCode.MaxTries])
    assert sessions.calls == 2
    rejected.connection_lost(None)

    now[0] = 60.0
    renewed, renewed_transport = _service(
        source="198.51.100.8",
        pairing_window=_OpenWindow(),
        pair_setup_attempt_limiter=attempts,
        server_session_factory=sessions,
    )
    renewed.data_received(_auth_frame(FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"}))
    assert not renewed_transport.closed
    assert sessions.calls == 3
    renewed.connection_lost(None)


def test_source_churn_exhausts_shared_global_m1_budget_without_srp():
    attempts = PairSetupAttemptLimiter(per_source_limit=5, global_limit=2)
    sessions = _CountingSessionFactory()

    for source in ("198.51.100.1", "198.51.100.2"):
        service, transport = _service(
            source=source,
            pairing_window=_OpenWindow(),
            pair_setup_attempt_limiter=attempts,
            server_session_factory=sessions,
        )
        service.data_received(_auth_frame(FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"}))
        assert not transport.closed
        service.connection_lost(None)

    rejected, rejected_transport = _service(
        source="198.51.100.3",
        pairing_window=_OpenWindow(),
        pair_setup_attempt_limiter=attempts,
        server_session_factory=sessions,
    )
    rejected.data_received(_auth_frame(FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"}))
    assert rejected_transport.closed
    assert _last_setup_response(rejected_transport)[TlvValue.Error] == bytes([ErrorCode.Busy])
    assert sessions.calls == 2
    rejected.connection_lost(None)


def test_successful_pair_setup_is_not_refunded_or_recorded_as_a_failure():
    attempts = PairSetupAttemptLimiter(per_source_limit=1, global_limit=20)
    failures = PairFailureLimiter(per_source_limit=1, global_limit=20)
    sessions = _CompletingSessionFactory()

    accepted, accepted_transport = _service(
        source="198.51.100.9",
        pairing_window=_OpenWindow(),
        pair_setup_attempt_limiter=attempts,
        pair_failure_limiter=failures,
        server_session_factory=sessions,
    )
    assert accepted._m1_setup({})
    assert accepted._m3_setup({TlvValue.PublicKey: b"", TlvValue.Proof: b""})
    session_key = hkdf_expand(
        "Pair-Setup-Encrypt-Salt",
        "Pair-Setup-Encrypt-Info",
        b"\x00" * 32,
    )
    encrypted = chacha20.Chacha20Cipher(session_key, session_key).encrypt(
        write_tlv({}), nonce=b"PS-Msg05"
    )
    assert accepted._m5_setup({TlvValue.EncryptedData: encrypted})
    assert not accepted_transport.closed
    assert failures.check("198.51.100.9").allowed
    accepted.connection_lost(None)

    rejected, rejected_transport = _service(
        source="198.51.100.9",
        pairing_window=_OpenWindow(),
        pair_setup_attempt_limiter=attempts,
        pair_failure_limiter=failures,
        server_session_factory=sessions,
    )
    rejected.data_received(_auth_frame(FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"}))
    assert rejected_transport.closed
    assert _last_setup_response(rejected_transport)[TlvValue.Error] == bytes([ErrorCode.MaxTries])
    assert sessions.calls == 1
    assert failures.check("198.51.100.9").allowed
    rejected.connection_lost(None)


def test_malformed_pre_m1_uses_the_bounded_failure_budget_without_srp():
    now = [0.0]
    attempts = PairSetupAttemptLimiter(clock=lambda: now[0])
    failures = PairFailureLimiter(
        per_source_limit=1,
        global_limit=20,
        window_seconds=60,
        clock=lambda: now[0],
    )
    sessions = _CountingSessionFactory()
    malformed, malformed_transport = _service(
        source="198.51.100.10",
        pairing_window=_OpenWindow(),
        pair_setup_attempt_limiter=attempts,
        pair_failure_limiter=failures,
        server_session_factory=sessions,
    )
    malformed.data_received(_frame(FrameType.PS_Start, opack.pack({"_pd": b"\x06"})))
    assert malformed_transport.closed
    assert sessions.calls == 0
    assert not failures.check("198.51.100.10").allowed
    malformed.connection_lost(None)

    backed_off, backed_off_transport = _service(
        source="198.51.100.10",
        pairing_window=_OpenWindow(),
        pair_setup_attempt_limiter=attempts,
        pair_failure_limiter=failures,
        server_session_factory=sessions,
    )
    backed_off.data_received(_auth_frame(FrameType.PS_Start, {TlvValue.SeqNo: b"\x01"}))
    response = _last_setup_response(backed_off_transport)
    assert backed_off_transport.closed
    assert response[TlvValue.Error] == bytes([ErrorCode.BackOff])
    assert sessions.calls == 0
    assert failures.check("198.51.100.10").retry_after == 60
    backed_off.connection_lost(None)


def test_pair_setup_attempt_limiter_bounds_source_keys_and_unknown_sources():
    now = [0.0]
    limiter = PairSetupAttemptLimiter(
        per_source_limit=1,
        global_limit=20,
        window_seconds=60,
        max_sources=2,
        clock=lambda: now[0],
    )
    assert limiter.admit(None).allowed
    assert limiter.admit("").error is ErrorCode.MaxTries

    for source in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
        assert limiter.admit(source).allowed
    assert limiter.tracked_sources == 2

    now[0] = 60.0
    assert limiter.admit("198.51.100.4").allowed
    assert limiter.tracked_sources == 1


class _AuthRecorder(CompanionServerAuth):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: list[tuple[FrameType, object]] = []

    def send_to_client(self, frame_type, data):
        self.sent.append((frame_type, data))

    def enable_encryption(self, output_key, input_key):
        pass


def test_srp_setup_is_deferred_until_pair_setup_m1():
    class _OpenWindow:
        @staticmethod
        def active():
            return type("Window", (), {"pin": "5718"})()

    with patch("atvr4samsung.companion.protocol.auth.new_server_session") as new_session:
        new_session.return_value = (type("Session", (), {"public": "01"})(), "00")
        auth = _AuthRecorder("device", pairing_window=_OpenWindow())
        assert new_session.call_count == 0
        auth._m1_setup({})
        assert new_session.call_count == 1


def test_throttled_pair_setup_returns_hap_backoff_without_srp_work():
    class _BackedOffAuth(_AuthRecorder):
        def pair_setup_backoff(self):
            return 17

    with patch("atvr4samsung.companion.protocol.auth.new_server_session") as new_session:
        auth = _BackedOffAuth("device")
        auth._m1_setup({})
        new_session.assert_not_called()
    response = read_tlv(auth.sent[-1][1]["_pd"])
    assert response[TlvValue.Error] == bytes([ErrorCode.BackOff])
    assert int.from_bytes(response[TlvValue.BackOff], byteorder="little") == 17


def test_malformed_pair_setup_sequence_counts_toward_backoff_without_srp_work():
    class _OpenWindow:
        @staticmethod
        def active():
            return type("Window", (), {"pin": "5718", "expires_at": 600.0})()

    class _LimitedAuth(_AuthRecorder):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._limiter = PairFailureLimiter(per_source_limit=2, global_limit=20)

        def pair_setup_backoff(self):
            decision = self._limiter.check("198.51.100.8")
            return decision.retry_after if not decision.allowed else 0

        def pairing_failed(self):
            self._limiter.record_failure("198.51.100.8")

    auth = _LimitedAuth("device", pairing_window=_OpenWindow())
    malformed_m1 = {"_pd": write_tlv({TlvValue.SeqNo: b"\x01"})}

    with patch("atvr4samsung.companion.protocol.auth.new_server_session") as new_session:
        auth.handle_auth_frame(FrameType.PS_Next, malformed_m1)
        auth.handle_auth_frame(FrameType.PS_Next, malformed_m1)
        auth._m1_setup({})
        new_session.assert_not_called()

    response = read_tlv(auth.sent[-1][1]["_pd"])
    assert response[TlvValue.Error] == bytes([ErrorCode.BackOff])


def test_protocol_logs_only_metadata_not_opack_contents(caplog):
    service, transport = _service()
    service.chacha = _PassthroughCipher()
    secret = "pin-proof-key-text"
    message = {"_i": "_secret", "_x": 1, "_t": 2, "_c": {"_tiD": b"\x00\xff", "text": secret}}

    with caplog.at_level("DEBUG"):
        service.send_to_client(FrameType.E_OPACK, message)
        service.data_received(_frame(FrameType.E_OPACK, opack.pack(message)))

    rendered = "\n".join(caplog.messages)
    assert secret not in rendered
    assert "_tiD" not in rendered
    assert "00ff" not in rendered
    assert transport.writes

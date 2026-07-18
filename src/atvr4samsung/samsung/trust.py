"""Explicit Samsung TLS certificate pinning and administrative bootstrap support.

The Frame commonly serves a self-signed certificate.  Normal bridge connections therefore trust only
the exact operator-approved certificate stored in the state directory; they never use an unverified
TLS context.  The separate bootstrap fetch deliberately performs only a TLS handshake, sends no
WebSocket request or token, and cannot persist anything without the CLI's explicit fingerprint
approval.
"""
from __future__ import annotations

import binascii
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import socket
import ssl
from typing import Any, Callable

from ..companion.protocol.atomic_io import durable_atomic_write_text, read_private_state_text


CERTIFICATE_PIN_FILENAME = "samsung-tls-cert.pem"


class SamsungTlsTrustError(RuntimeError):
    """The Samsung TLS pin is absent, unsafe, invalid, or does not match the live connection."""


@dataclass(frozen=True)
class TrustedSamsungCertificate:
    """Canonical PEM, DER, and SHA-256 fingerprint for one approved TV certificate."""

    pem: str
    der: bytes
    sha256: str


def certificate_sha256(der: bytes) -> str:
    """Return the stable lowercase SHA-256 fingerprint for one DER certificate."""
    return hashlib.sha256(der).hexdigest()


def certificate_from_der(der: bytes) -> TrustedSamsungCertificate:
    """Normalize one DER X.509 certificate into the persisted pin representation."""
    if not isinstance(der, bytes) or not der:
        raise SamsungTlsTrustError("Samsung TLS peer did not provide a certificate")
    try:
        pem = ssl.DER_cert_to_PEM_cert(der)
    except ValueError as exc:
        raise SamsungTlsTrustError("Samsung TLS peer provided an invalid certificate") from exc
    return TrustedSamsungCertificate(pem=pem, der=der, sha256=certificate_sha256(der))


def certificate_from_pem(pem: str) -> TrustedSamsungCertificate:
    """Validate and canonicalize one PEM X.509 certificate."""
    if (
        pem.count("-----BEGIN CERTIFICATE-----") != 1
        or pem.count("-----END CERTIFICATE-----") != 1
    ):
        raise SamsungTlsTrustError("Samsung TLS certificate pin must contain exactly one PEM certificate")
    try:
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise SamsungTlsTrustError("Samsung TLS certificate pin is not valid PEM") from exc
    certificate = certificate_from_der(der)
    if pem.strip() != certificate.pem.strip():
        raise SamsungTlsTrustError("Samsung TLS certificate pin must contain exactly one PEM certificate")
    return certificate


def trust_file_for_state_dir(state_dir: Path) -> Path:
    """Return the fixed, gitignored Samsung certificate-pin location for one installation."""
    return state_dir / CERTIFICATE_PIN_FILENAME


def load_trusted_certificate(path: Path) -> TrustedSamsungCertificate:
    """Load one operator-approved 0600 certificate pin, failing closed on any unsafe state."""
    path = Path(path)
    try:
        pem = read_private_state_text(path, encoding="ascii").text
    except FileNotFoundError as exc:
        raise SamsungTlsTrustError(
            f"Samsung TLS certificate pin is missing at {path}; run `atvr4samsung trust-tv`"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise SamsungTlsTrustError(
            f"could not securely read Samsung TLS certificate pin at {path}: {exc}"
        ) from exc
    certificate = certificate_from_pem(pem)
    create_pinned_ssl_context(certificate)
    return certificate


def persist_trusted_certificate(
    path: Path, certificate: TrustedSamsungCertificate
) -> TrustedSamsungCertificate:
    """Strictly and atomically replace the exact approved PEM certificate pin with mode 0600."""
    validated = certificate_from_pem(certificate.pem)
    if not hmac.compare_digest(validated.der, certificate.der):
        raise SamsungTlsTrustError("Samsung TLS certificate pin data is internally inconsistent")
    create_pinned_ssl_context(validated)
    durable_atomic_write_text(path, validated.pem, mode=0o600)
    # The strict descriptor-relative write has already committed the exact canonical PEM above. Avoid
    # reopening the pathname here: an ancestor replacement must not make this administrative command
    # report a different certificate than the one it safely persisted.
    return validated


def create_pinned_ssl_context(certificate: TrustedSamsungCertificate) -> ssl.SSLContext:
    """Create a TLS context that trusts only the approved certificate.

    Samsung's self-signed certificate normally does not name its LAN IP, so hostname matching is not
    meaningful.  Certificate-chain validation remains mandatory and the actual websocket connection
    is additionally compared byte-for-byte with this exact certificate after its TLS handshake.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        context.load_verify_locations(cadata=certificate.pem)
    except ssl.SSLError as exc:
        raise SamsungTlsTrustError("could not load the approved Samsung TLS certificate pin") from exc
    # Frames present the approved non-CA leaf plus Samsung's self-signed issuer. Treat only that
    # explicitly approved leaf as a trust anchor; the live peer is still compared byte-for-byte.
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return context


def verify_connected_certificate(
    connection: Any, certificate: TrustedSamsungCertificate
) -> None:
    """Verify that the already-established websocket's TLS peer is exactly the approved certificate."""
    transport = getattr(connection, "transport", None)
    if transport is None:
        transport = getattr(connection, "_transport", None)
    if transport is None or not hasattr(transport, "get_extra_info"):
        raise SamsungTlsTrustError("Samsung websocket did not expose its TLS transport")
    tls_socket = transport.get_extra_info("ssl_object")
    if tls_socket is None:
        raise SamsungTlsTrustError("Samsung websocket is not protected by TLS")
    try:
        peer_der = tls_socket.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise SamsungTlsTrustError("could not read the Samsung websocket peer certificate") from exc
    if not isinstance(peer_der, bytes) or not peer_der:
        raise SamsungTlsTrustError("Samsung websocket peer did not provide a certificate")
    if not hmac.compare_digest(peer_der, certificate.der):
        raise SamsungTlsTrustError(
            "Samsung TLS certificate changed or does not match the approved pin; "
            "inspect it and run `atvr4samsung trust-tv` again"
        )


def _bootstrap_ssl_context() -> ssl.SSLContext:
    """Make the intentionally unverified, token-free TLS context used only by `trust-tv`."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    # This is never available to the bridge transport. It retrieves a public certificate before the
    # administrator compares and explicitly approves its fingerprint; no HTTP/WebSocket data is sent.
    context.verify_mode = ssl.CERT_NONE
    return context


SocketFactory = Callable[..., Any]
SslContextFactory = Callable[[], ssl.SSLContext]


def fetch_tv_certificate(
    host: str,
    *,
    port: int = 8002,
    timeout: float = 10.0,
    socket_factory: SocketFactory = socket.create_connection,
    ssl_context_factory: SslContextFactory = _bootstrap_ssl_context,
) -> TrustedSamsungCertificate:
    """Fetch the public certificate over a token-free TLS handshake for explicit admin review."""
    context = ssl_context_factory()
    try:
        with socket_factory((host, port), timeout=timeout) as raw_socket:
            with context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
                peer_der = tls_socket.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise SamsungTlsTrustError(
            f"could not fetch Samsung TLS certificate ({type(exc).__name__})"
        ) from exc
    return certificate_from_der(peer_der)

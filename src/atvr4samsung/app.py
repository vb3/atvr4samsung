"""Console entry point for the bridge.

``atvr4samsung --check`` validates config and prints resolved settings (no network needed).
``atvr4samsung`` runs the bridge: start the emulated Apple TV (Companion server), advertise it via
mDNS so the iPhone can pair, then relay each command to the Samsung TV (connecting on demand). Validated
end-to-end against a real iPhone (iOS 26). See docs/hld.md / docs/operations.md.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import logging
import os
import pathlib
import signal
import socket
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, TypeVar

from .config import Config, load_config
from .pairing_window import DEFAULT_WINDOW_SECONDS, PairingWindowStore

if TYPE_CHECKING:
    from .companion.protocol.paired_clients import PairedClients
    from .companion.protocol.server_identity import ServerIdentity

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("atvr4samsung")
except Exception:  # not installed as a distribution (bare source tree)
    __version__ = "0.0.0+unknown"

# Default config location (XDG-style). The CLI uses this when --config is omitted so `run`/`init`/
# `--check`/`trust-tv`/`pair` all agree.
DEFAULT_CONFIG_PATH = "~/.config/atvr4samsung/config.yaml"

_LOGGER = logging.getLogger(__name__)
_ListenerResult = TypeVar("_ListenerResult")


def _detect_local_ip(target_host: str) -> str:
    """Best-effort local IP that would be used to reach ``target_host`` (no packets sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target_host, 9))
        return sock.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        sock.close()


def _print_check(config: Config) -> bool:
    print("atvr4samsung — resolved configuration")
    print(f"  Apple side : advertise {config.companion.device_name!r} "
          f"(model {config.companion.model}) on TCP port {config.companion.port}")
    print(f"  Samsung    : {config.samsung.host}:{config.samsung.port} "
          f"(name {config.samsung.name!r}, token_file={config.samsung.token_file})")
    print(f"  Wake-on-LAN: enabled={config.samsung.wol.enabled} mac={config.samsung.mac} "
          f"via {config.samsung.wol.broadcast}:{config.samsung.wol.port}")
    print(f"  Local IP   : {_detect_local_ip(config.samsung.host)}")
    if config.companion.state_dir is None:
        print("  State dir  : MISSING — required to run or manage controlled enrollment.")
        print("  Config is incomplete: add companion.state_dir before running the bridge.")
        return False
    else:
        print(f"  State dir  : {config.companion.state_dir}")
        print(
            "  Samsung TLS: requires a 0600 certificate pin at "
            f"{config.samsung_tls_certificate_file} "
            "(create it with `atvr4samsung trust-tv`)"
        )
    print("  Config looks valid. (This does not contact the TV or the phone; run `doctor` for that.)")
    return True


async def _start_companion_listener_with_identity(
    state_dir: pathlib.Path,
    create_listener: Callable[
        ["ServerIdentity", "PairedClients", PairingWindowStore],
        Awaitable[_ListenerResult],
    ],
) -> _ListenerResult:
    """Recover/load identity and bind its listener as one pairing-state transaction.

    The lock ends as soon as ``create_listener`` returns a listening server. This gives a reset one
    unambiguous outcome: it either recovers before startup chooses an identity, or it resets an
    already-listening daemon. The paired-client snapshot is constructed while locked as well, so a
    recovered clear cannot leave the new daemon with a stale initial mapping.
    """
    from .companion.protocol.paired_clients import PairedClients
    from .companion.protocol.pairing_state import async_pairing_state_lock
    from .companion.protocol.server_identity import load_or_create_server_identity_locked

    async with async_pairing_state_lock(state_dir):
        identity = load_or_create_server_identity_locked(state_dir)
        paired = PairedClients(state_dir / "paired-clients.json")
        pairing_window = PairingWindowStore(state_dir)
        try:
            return await create_listener(identity, paired, pairing_window)
        except BaseException:
            paired.close()
            raise


async def run(config: Config) -> None:
    if config.companion.state_dir is None:
        raise RuntimeError(
            "companion.state_dir is required to run: it holds the persistent identity, paired devices, "
            "and fail-closed enrollment window"
        )
    from .samsung.trust import load_trusted_certificate

    tls_certificate_file = config.samsung_tls_certificate_file
    assert tls_certificate_file is not None
    # Refuse to advertise a bridge that could only reach the TV through an unpinned transport. This
    # does not contact the TV; `trust-tv` is the sole administrative path that creates this state.
    load_trusted_certificate(tls_certificate_file)

    # Imported lazily so `--check` and tests don't require the runtime deps (samsungtvws).
    from zeroconf import Zeroconf

    from .bridge.gestures import GestureConfig
    from .companion.discovery import CompanionAdvertiser
    from .companion.relay import DirectionalHoldConfig
    from .companion.server import (
        close_server as close_companion_server,
        make_ime_focus_handler,
        make_samsung_dispatch,
        serve,
    )
    from .samsung.client import SamsungFrameClient, connect_failure_hint

    _LOGGER.info("Starting the bridge.")

    async with AsyncExitStack() as stack:
        client = SamsungFrameClient(
            host=config.samsung.host,
            mac=config.samsung.mac,
            port=config.samsung.port,
            name=config.samsung.name,
            token_file=config.samsung.token_file,
            tls_certificate_file=tls_certificate_file,
            wol_enabled=config.samsung.wol.enabled,
            wol_broadcast=config.samsung.wol.broadcast,
            wol_port=config.samsung.wol.port,
            key_press_delay=0.05,  # snappy pacing for rapid discrete swipes (validated on-device)
        )
        stack.push_async_callback(client.close)  # always clean up, even though connect is deferred

        dispatch = make_samsung_dispatch(client)
        # Swipe tuning (validated on-device): repeat_every=350 keeps a deliberate swipe = 1 step while
        # a fast full-width flick scrolls ~3; key_press_delay=0.05 lets a rapid burst drain smoothly.
        gesture_config = GestureConfig(repeat_every=350)
        # Swipe-and-hold auto-repeat (directional scroll): dwell ~400ms then repeat.
        hold_config = DirectionalHoldConfig(enabled=True)
        bound_identity_identifier: Optional[str] = None

        async def create_listener(server_identity, paired, pairing_window):
            nonlocal bound_identity_identifier
            listener = await serve(
                dispatch,
                host="0.0.0.0",
                port=config.companion.port,
                device_name=config.companion.device_name,
                gesture_config=gesture_config,
                hold_config=hold_config,
                unique_id=server_identity.identifier,
                private_key=server_identity.private_key,
                server_identity_generation=server_identity.generation,
                paired_clients=paired,
                require_paired=True,
                pairing_window=pairing_window,
            )
            bound_identity_identifier = server_identity.identifier
            return listener

        server, state = await _start_companion_listener_with_identity(
            config.companion.state_dir,
            create_listener,
        )
        assert bound_identity_identifier is not None
        # Mirror the TV's text-field focus to the iPhone keyboard (system fields only; see operations).
        client.set_ime_event_handler(make_ime_focus_handler(state))
        bound_port = server.sockets[0].getsockname()[1]

        server_closed = False

        async def close_server() -> None:
            nonlocal server_closed
            if not server_closed:
                server_closed = True
                await close_companion_server(server)

        loop = asyncio.get_event_loop()

        try:
            zconf = Zeroconf()
            # Close zeroconf OFF the event-loop thread on shutdown. zeroconf's own close() runs
            # unregister_all_services, which does blocking multicast I/O; called on the loop thread it
            # detects the running loop and logs "unregister_all_services skipped as it does blocking
            # i/o" then skips it. (Harmless today — advertiser.close, registered after this so it
            # unwinds first, already sent the goodbye packet — but the warning is pure noise.)
            async def _close_zeroconf() -> None:
                await loop.run_in_executor(None, zconf.close)

            stack.push_async_callback(_close_zeroconf)
            # The advertiser defers registration until a real LAN IPv4 exists and re-advertises if the
            # IP later changes (DHCP renewal / interface flap), so the iPhone keeps discovering us.
            advertiser = CompanionAdvertiser(
                loop, zconf, port=bound_port,
                device_name=config.companion.device_name,
                identity_identifier=bound_identity_identifier,
                model=config.companion.model,
                detect_ip=lambda: _detect_local_ip(config.samsung.host),
            )
            stack.push_async_callback(advertiser.close)
            await advertiser.start()
        except OSError as exc:
            await close_server()
            # Without mDNS the phone can't find us at all, so fail with guidance instead of a raw trace.
            raise RuntimeError(
                f"mDNS advertisement failed ({exc}). Check that UDP 5353 isn't blocked by a local "
                "firewall, that the interface allows multicast, and (on segmented VLANs) that an mDNS "
                "reflector forwards _companion-link._tcp from the phone's network to this host."
            ) from exc
        except BaseException:
            await close_server()
            raise
        # Stop Companion work before tearing down mDNS or the Samsung socket. This keeps a late
        # remote frame from racing shutdown and lets close_server drain the shared dispatch lane.
        stack.push_async_callback(close_server)

        # The Apple side is now up, so the iPhone can discover + pair even if the TV is offline.
        # Surface what the operator should look for (never the PIN).
        _LOGGER.info(
            "Advertising %r on Companion port %s — run `atvr4samsung pair` before enrolling a new "
            "iPhone. The Samsung 'Allow' prompt appears as %r. State dir: %s",
            config.companion.device_name, bound_port, config.samsung.name,
            config.companion.state_dir,
        )

        # Connect to the TV best-effort: a sleeping/unreachable TV (or a pending 'Allow' prompt) must
        # not crash the service. The relay reconnects on the first command once the TV is awake.
        try:
            await client.connect()
            _LOGGER.info("Connected to the Samsung TV at %s:%s.", config.samsung.host, config.samsung.port)
        except Exception as exc:
            _LOGGER.warning(
                "Samsung TV at %s:%s not reachable yet (%s); the remote will still pair, and commands "
                "will connect when the TV is awake. Accept the TV's 'Allow' prompt on first use.",
                config.samsung.host, config.samsung.port, connect_failure_hint(exc),
            )

        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # e.g. on platforms without signal support
                pass

        _LOGGER.info("Bridge running. Stop the service (or Ctrl-C) to exit.")
        try:
            await stop.wait()
        finally:
            _LOGGER.info("Shutting down")


def _cmd_init(path: str) -> int:
    import importlib.resources as ir
    from .companion.protocol.atomic_io import atomic_write_text

    dest = pathlib.Path(path).expanduser()
    if dest.exists():
        print(f"{dest} already exists — leaving it untouched.")
        return 0
    try:
        template = ir.files("atvr4samsung").joinpath("config.example.yaml").read_text()
    except (FileNotFoundError, ModuleNotFoundError):
        template = (
            "companion:\n"
            "  device_name: \"Frame Living Room\"\n"
            "  state_dir: \"~/.local/state/atvr4samsung\"\n"
            "samsung:\n"
            "  host: \"\"\n"
            "  mac: \"\"\n"
        )

    atomic_write_text(dest, template, mode=0o600)
    print(f"Wrote {dest}.")
    print(
        "Next: set samsung.host/mac, run `atvr4samsung --check`, then "
        "`atvr4samsung trust-tv` before `doctor` or starting the service."
    )
    return 0


def _probe_bind(port: int) -> tuple[bool, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
        return True, f"port {port} is free to bind"
    except OSError as exc:
        return False, f"cannot bind port {port} ({exc}) — another process may already use it"
    finally:
        sock.close()


def _probe_writable_dir(path) -> tuple[bool, str]:
    target = pathlib.Path(path).expanduser()
    from .companion.protocol.atomic_io import probe_durable_directory

    try:
        target = probe_durable_directory(target)
        return True, f"{target} is writable"
    except (OSError, ValueError) as exc:
        return False, f"{target} not writable ({exc})"


def _probe_zeroconf() -> tuple[bool, str]:
    try:
        from zeroconf import Zeroconf
    except ImportError as exc:
        return False, f"zeroconf not installed ({exc})"
    try:
        zconf = Zeroconf()
        zconf.close()
        return True, "can open multicast-DNS sockets"
    except OSError as exc:
        return False, f"could not start mDNS ({exc}) — check UDP 5353 / multicast on this interface"


async def _probe_tv(config: Config) -> tuple[bool, str]:
    from .samsung.client import SamsungFrameClient, connect_failure_hint

    client = SamsungFrameClient(
        host=config.samsung.host, mac=config.samsung.mac, port=config.samsung.port,
        name=config.samsung.name, token_file=config.samsung.token_file,
        tls_certificate_file=config.samsung_tls_certificate_file, connect_timeout=5.0,
    )
    try:
        await client.connect()
        return True, f"connected to {config.samsung.host}:{config.samsung.port}"
    except Exception as exc:
        return (
            False,
            "not reachable now "
            f"({connect_failure_hint(exc)}) — OK if the TV is asleep; it wakes on first command",
        )
    finally:
        await client.close()


async def _cmd_doctor(config: Config) -> int:
    """Network-aware preflight: complements `--check` (which never touches the network)."""
    results: list[tuple[str, bool, bool, str]] = []  # (label, ok, warn_only, detail)

    def add(label: str, ok: bool, detail: str, *, warn_only: bool = False) -> None:
        results.append((label, ok, warn_only, detail))

    host, mac = config.samsung.host, config.samsung.mac
    add("Samsung host set", host not in ("", "192.168.1.50"),
        host if host not in ("", "192.168.1.50") else f"{host or '(unset)'} — still the example value")
    add("Samsung MAC set", mac.upper() not in ("", "AA:BB:CC:DD:EE:FF"),
        mac if mac.upper() not in ("", "AA:BB:CC:DD:EE:FF") else f"{mac or '(unset)'} — needed for Wake-on-LAN")

    local_ip = _detect_local_ip(config.samsung.host)
    add("Local LAN IP", local_ip != "0.0.0.0",
        local_ip if local_ip != "0.0.0.0" else "could not determine (interface down / no IPv4?)")

    if config.companion.port == 0:
        add("Companion port", True, "ephemeral (0) — chosen at runtime", warn_only=True)
    else:
        add("Companion port", *_probe_bind(config.companion.port))

    if config.companion.state_dir:
        add("State dir writable", *_probe_writable_dir(config.companion.state_dir))
    else:
        add("State dir", False, "required for persistent identity, paired devices, and enrollment")

    if config.samsung.token_file:
        add("Samsung token path", *_probe_writable_dir(pathlib.Path(config.samsung.token_file).parent))

    tls_pin_ready = False
    if config.samsung_tls_certificate_file is None:
        add("Samsung TLS certificate pin", False, "requires companion.state_dir")
    else:
        from .samsung.trust import SamsungTlsTrustError, load_trusted_certificate

        add(
            "Samsung TLS pin parent",
            *_probe_writable_dir(config.samsung_tls_certificate_file.parent),
        )
        try:
            load_trusted_certificate(config.samsung_tls_certificate_file)
        except SamsungTlsTrustError as exc:
            add(
                "Samsung TLS certificate pin",
                False,
                f"{exc}; run `atvr4samsung trust-tv`",
            )
        else:
            tls_pin_ready = True
            add(
                "Samsung TLS certificate pin",
                True,
                f"{config.samsung_tls_certificate_file} is a readable 0600 pin",
            )

    add("mDNS (Zeroconf)", *await asyncio.get_event_loop().run_in_executor(None, _probe_zeroconf))
    if tls_pin_ready:
        add("Samsung TV reachable", *(await _probe_tv(config)), warn_only=True)
    else:
        add(
            "Samsung TV reachable",
            True,
            "skipped until the required certificate pin is created",
            warn_only=True,
        )

    print("atvr4samsung doctor — network preflight\n")
    for label, ok, warn_only, detail in results:
        mark = "OK  " if ok else ("WARN" if warn_only else "FAIL")
        print(f"  [{mark}] {label}: {detail}")
    print()

    if any(not ok and not warn_only for _, ok, warn_only, _ in results):
        print("Problems found (FAIL above). Fix them, then re-run `atvr4samsung doctor`.")
        return 1
    print("All checks passed (warnings are non-fatal). Ready to run.")
    return 0


def _cmd_healthcheck(config: Config, *, timeout: float = 2.0) -> int:
    """Validate persistent trust and confirm the local Companion listener accepts TCP."""
    if config.companion.port == 0:
        print("error: healthcheck requires a fixed companion.port")
        return 1
    tls_pin = config.samsung_tls_certificate_file
    if tls_pin is None:
        print("error: healthcheck requires companion.state_dir")
        return 1

    from .samsung.trust import SamsungTlsTrustError, load_trusted_certificate

    try:
        load_trusted_certificate(tls_pin)
        with socket.create_connection(("127.0.0.1", config.companion.port), timeout=timeout):
            pass
    except (OSError, SamsungTlsTrustError) as exc:
        print(f"error: healthcheck failed: {exc}")
        return 1
    print("healthy")
    return 0


def _cmd_trust_tv(
    config: Config,
    *,
    approved_sha256: Optional[str] = None,
    fetcher=None,
) -> int:
    """Fetch a token-free TLS certificate for review and persist it only after exact approval."""
    from .samsung.trust import (
        SamsungTlsTrustError,
        fetch_tv_certificate,
        persist_trusted_certificate,
    )

    state_dir = _state_dir_for_management(config)
    if state_dir is None:
        return 2
    pin_path = config.samsung_tls_certificate_file
    assert pin_path is not None
    fetch = fetcher or fetch_tv_certificate
    try:
        certificate = fetch(config.samsung.host, port=config.samsung.port)
    except SamsungTlsTrustError as exc:
        print(f"error: {exc}")
        return 1

    print(f"Fetched Samsung TLS certificate SHA-256: {certificate.sha256}")
    print("No token or WebSocket request was sent.")
    if approved_sha256 is None:
        print("No certificate pin was written.")
        print(
            "Inspect the fingerprint, then approve this exact certificate with:\n"
            f"  atvr4samsung trust-tv --approve-sha256 {certificate.sha256}"
        )
        return 0

    approved = approved_sha256.replace(":", "").strip().lower()
    if approved != certificate.sha256:
        print("error: --approve-sha256 does not match the fetched certificate; no pin was written.")
        return 1
    try:
        persist_trusted_certificate(pin_path, certificate)
    except (OSError, SamsungTlsTrustError) as exc:
        print(f"error: could not save Samsung TLS certificate pin: {exc}")
        return 1
    print(f"Saved approved Samsung TLS certificate pin at {pin_path} (mode 0600).")
    print("Restart the service if it is running so its next connection uses this pin.")
    return 0


def _state_dir_for_management(config: Config) -> Optional[pathlib.Path]:
    if config.companion.state_dir is None:
        print(
            "error: companion.state_dir is required for controlled enrollment and paired-device "
            "management; add it to config.yaml and retry."
        )
        return None
    return config.companion.state_dir


def _cmd_pair(config: Config, *, duration_seconds: float = DEFAULT_WINDOW_SECONDS) -> int:
    """Open a fresh, temporary enrollment window and print its PIN exactly once."""
    from .companion.protocol.identity_reset import IdentityResetInProgressError
    from .companion.protocol.server_identity import (
        MissingServerIdentityError,
        ServerIdentityError,
        load_persisted_identity_locked,
    )

    state_dir = _state_dir_for_management(config)
    if state_dir is None:
        return 2
    store = PairingWindowStore(state_dir)
    try:
        # Keep identity lookup and publication under the same lock as reset/M5. `pair` must not
        # manufacture an identity or race a reset into naming a daemon that is no longer current.
        with store.transaction():
            identity = load_persisted_identity_locked(state_dir)
            window = store.open_locked(
                server_identifier=identity.identifier,
                server_generation=identity.generation,
                duration_seconds=duration_seconds,
            )
    except IdentityResetInProgressError:
        print(
            "error: a pairing-state clear or reset is pending; restart the service to finish recovery before "
            "retrying `atvr4samsung pair`."
        )
        return 1
    except MissingServerIdentityError:
        print(
            "error: no persisted server identity; start or restart the service, then retry "
            "`atvr4samsung pair`."
        )
        return 1
    except ServerIdentityError:
        print(
            "error: the persisted server identity is corrupt or unreadable; run "
            "`atvr4samsung unpair --reset-identity` (or restore a known-good identity), then restart "
            "the service before retrying `atvr4samsung pair`."
        )
        return 1
    except OSError:
        # Either identity or window metadata can be visible despite a failed directory fsync. Do not
        # reveal a PIN (or report success) until all strict pairing-state commits have returned.
        print("error: pairing state was not durably committed; retry `atvr4samsung pair`.")
        return 1
    except ValueError:
        print("error: enrollment window duration is invalid.")
        return 1
    expiry = datetime.fromtimestamp(window.expires_at).astimezone().isoformat(timespec="seconds")
    print(f"Enrollment is open until {expiry}.")
    print(f"Pairing PIN: {window.pin}")
    print("Pair each new iPhone from Control Center → Apple TV Remote before expiry.")
    return 0


def _cmd_pairs(config: Config) -> int:
    """List paired controller identifiers without exposing their public keys."""
    from .companion.protocol.paired_clients import MAX_PAIRED_CLIENTS, PairedClients, PairedClientsError

    state_dir = _state_dir_for_management(config)
    if state_dir is None:
        return 2
    try:
        with PairedClients(state_dir / "paired-clients.json") as store:
            identifiers = store.identifiers()
    except (OSError, PairedClientsError) as exc:
        print(f"error: {exc}")
        return 1
    if not identifiers:
        print(f"No paired devices (0/{MAX_PAIRED_CLIENTS}).")
        return 0
    print(f"Paired devices ({len(identifiers)}/{MAX_PAIRED_CLIENTS}):")
    for identifier in identifiers:
        print(f"  {identifier}")
    return 0


def _cmd_revoke(config: Config, identifier: str) -> int:
    """Revoke one exact controller identifier."""
    from .companion.protocol.paired_clients import PairedClients, PairedClientsError

    state_dir = _state_dir_for_management(config)
    if state_dir is None:
        return 2
    if not identifier:
        print("error: revoke requires a paired-device identifier; run `atvr4samsung pairs` to list them.")
        return 2
    try:
        with PairedClients(state_dir / "paired-clients.json") as store:
            removed = store.remove(identifier)
    except (OSError, PairedClientsError) as exc:
        print(f"error: {exc}")
        return 1
    if removed:
        print(f"Revoked paired device {identifier!r}.")
    else:
        print(f"No paired device matches {identifier!r}.")
    return 0


def _cmd_unpair(config: Config, *, reset_identity_too: bool = False) -> int:
    from .companion.protocol.identity_reset import (
        begin_clear_all_locked,
        begin_identity_reset_locked,
        clear_clear_all_locked,
    )
    from .companion.protocol.paired_clients import PairedClients, PairedClientsError
    from .companion.protocol.server_identity import reset_identity_locked

    state_dir = config.companion.state_dir
    if state_dir is None:
        print("No companion.state_dir configured — no controlled enrollment state exists to clear.")
        return 0

    clear_all_checkpoint_owned = False
    try:
        # M5 revalidates and persists under this same transaction lock. Do not release it between
        # closing enrollment, deleting clients, and deleting the server identity, or a verified M5
        # could repopulate the store or a new window could name an identity being reset.
        with PairingWindowStore(state_dir).transaction():
            if reset_identity_too:
                # This strict checkpoint must be durable before deleting any authorization state. A
                # surviving daemon observes it immediately, and startup replays the sole reset path.
                begin_identity_reset_locked(state_dir)
            else:
                # The common fence revokes live authorization before either unlink. Startup
                # distinguishes the operation and preserves this identity after completing the clear.
                clear_all_checkpoint_owned = begin_clear_all_locked(state_dir)
            window_cleared = PairingWindowStore.clear_state_locked(state_dir)
            clients_cleared = PairedClients.clear_state_locked(state_dir / "paired-clients.json")
            identity_cleared = (
                reset_identity_locked(state_dir) if reset_identity_too else False
            )
            if not reset_identity_too and clear_all_checkpoint_owned:
                clear_clear_all_locked(state_dir)

        if window_cleared:
            print("Closed the active enrollment window.")

        if clients_cleared:
            print("Cleared paired iPhone(s).")
        else:
            print("No paired clients on file.")

        if reset_identity_too:
            if identity_cleared:
                print(
                    "Removed persisted server identity. Restart the service before opening a new "
                    "enrollment window; it will create a new Apple TV identity."
                )
            else:
                print("No server identity on file to reset.")
    except (OSError, PairedClientsError) as exc:
        print(f"error: pairing state was not durably cleared: {exc}")
        return 1

    print("The Samsung token and TLS pin were left untouched.")
    if reset_identity_too or not clear_all_checkpoint_owned:
        print(
            "Restart the service so it creates and advertises a replacement Apple TV identity, then "
            "run `atvr4samsung pair` and select the remote again on the iPhone."
        )
    else:
        print("If the service is running, revocation takes effect before the old phone's next command; "
              "no restart is required. Re-enrolling that same phone requires an identity reset because "
              "iOS Control Center does not expose a remove-pairing action.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="atvr4samsung", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help=f"path to config.yaml (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--check", action="store_true",
                        help="validate config and print resolved settings, then exit (no network)")
    parser.add_argument("--reset-identity", action="store_true",
                        help="with unpair: reset the Apple TV identity through a crash-safe checkpoint "
                             "(restart the service, then forget and re-pair the iPhone)")
    parser.add_argument("--minutes", type=float, default=DEFAULT_WINDOW_SECONDS / 60,
                        help="with pair: enrollment window length in minutes (default: 5)")
    parser.add_argument(
        "--approve-sha256",
        help="with trust-tv: exact SHA-256 fingerprint to approve and persist as the TV TLS pin",
    )
    parser.add_argument("command", nargs="?",
                        choices=["run", "init", "doctor", "healthcheck", "trust-tv", "pair", "pairs",
                                 "revoke", "unpair"],
                        default="run",
                        help="run (default), init config, doctor (network preflight), healthcheck "
                             "(local listener readiness), trust-tv (review/approve the Samsung TLS pin), "
                             "pair (open enrollment), pairs (list devices), revoke <identifier>, "
                             "or unpair (clear all devices)")
    parser.add_argument("identifier", nargs="?", help="with revoke: exact paired-device identifier")
    args = parser.parse_args()
    # Expand ~ / $VARS once so every subcommand (and the default config path) resolves consistently.
    args.config = os.path.expanduser(os.path.expandvars(args.config))

    if args.command == "init":
        return _cmd_init(args.config)
    if args.identifier and args.command != "revoke":
        parser.error("a paired-device identifier is only valid with `revoke`")
    if args.approve_sha256 and args.command != "trust-tv":
        parser.error("--approve-sha256 is only valid with `trust-tv`")

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}\nRun `atvr4samsung init` to create a config.")
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.check:
        return 0 if _print_check(config) else 1

    if args.command == "doctor":
        return asyncio.run(_cmd_doctor(config))
    if args.command == "healthcheck":
        return _cmd_healthcheck(config)
    if args.command == "trust-tv":
        return _cmd_trust_tv(config, approved_sha256=args.approve_sha256)
    if args.command == "pair":
        return _cmd_pair(config, duration_seconds=args.minutes * 60)
    if args.command == "pairs":
        return _cmd_pairs(config)
    if args.command == "revoke":
        return _cmd_revoke(config, args.identifier or "")
    if args.command == "unpair":
        return _cmd_unpair(config, reset_identity_too=args.reset_identity)

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # don't dump a raw traceback at an operator; the supervisor sees non-zero
        _LOGGER.error("Bridge stopped with an error: %s", exc)
        _LOGGER.debug("Fatal error detail", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

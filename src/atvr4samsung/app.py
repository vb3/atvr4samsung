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
import re
import secrets
import shutil
import signal
import socket
import sys
from typing import Optional

from .config import Config, load_config, pin_is_weak

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("atvr4samsung")
except Exception:  # not installed as a distribution (bare source tree)
    __version__ = "0.0.0+unknown"

# Default config location (XDG-style). The CLI uses this when --config is omitted so `run`/`init`/
# `--check`/`install-service` all agree with the installer and docs.
DEFAULT_CONFIG_PATH = "~/.config/atvr4samsung/config.yaml"

_LOGGER = logging.getLogger(__name__)


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


def _print_check(config: Config) -> None:
    print("atvr4samsung — resolved configuration")
    print(f"  Apple side : advertise {config.companion.device_name!r} "
          f"(model {config.companion.model}) on TCP port {config.companion.port}")
    print(f"  Samsung    : {config.samsung.host}:{config.samsung.port} "
          f"(name {config.samsung.name!r}, token_file={config.samsung.token_file})")
    print(f"  Wake-on-LAN: enabled={config.samsung.wol.enabled} mac={config.samsung.mac} "
          f"via {config.samsung.wol.broadcast}:{config.samsung.wol.port}")
    print(f"  Local IP   : {_detect_local_ip(config.samsung.host)}")
    if pin_is_weak(config.companion.pin):
        print("  PIN        : WEAK — set a stronger companion.pin before a public deployment.")
    print("  Config looks valid. (This does not contact the TV or the phone; run `doctor` for that.)")


async def run(config: Config) -> None:
    if pin_is_weak(config.companion.pin):
        _LOGGER.warning(
            "Configured pairing PIN is weak or a default; set a stronger PIN in config.yaml "
            "(companion.pin) for a public-facing deployment."
        )

    # Imported lazily so `--check` and tests don't require the runtime deps (samsungtvws).
    from zeroconf import Zeroconf

    from .companion.discovery import advertise_companion
    from .companion.protocol.paired_clients import PairedClients
    from .companion.protocol.server_identity import load_or_create_identity
    from .companion.server import make_samsung_dispatch, serve
    from .samsung.client import SamsungFrameClient

    _LOGGER.info("Starting the bridge.")

    server_uuid, private_key = load_or_create_identity(config.companion.state_dir)
    paired = PairedClients(
        config.companion.state_dir / "paired-clients.json" if config.companion.state_dir else None
    )

    async with AsyncExitStack() as stack:
        client = SamsungFrameClient(
            host=config.samsung.host,
            mac=config.samsung.mac,
            port=config.samsung.port,
            name=config.samsung.name,
            token_file=config.samsung.token_file,
            wol_enabled=config.samsung.wol.enabled,
            wol_broadcast=config.samsung.wol.broadcast,
            wol_port=config.samsung.wol.port,
        )
        stack.push_async_callback(client.close)  # always clean up, even though connect is deferred

        dispatch = make_samsung_dispatch(client)
        server, _state = await serve(
            dispatch,
            host="0.0.0.0",
            port=config.companion.port,
            device_name=config.companion.device_name,
            pin=int(config.companion.pin),
            unique_id=server_uuid,
            private_key=private_key,
            paired_clients=paired,
            require_paired=True,
        )
        bound_port = server.sockets[0].getsockname()[1]

        async def close_server() -> None:
            server.close()
            await server.wait_closed()

        stack.push_async_callback(close_server)

        loop = asyncio.get_event_loop()
        local_ip = _detect_local_ip(config.samsung.host)
        if local_ip == "0.0.0.0":
            # Advertising 0.0.0.0 over mDNS makes us undiscoverable; the iPhone needs a real LAN IP.
            _LOGGER.warning(
                "Could not determine this host's LAN IP (got 0.0.0.0); the iPhone may not discover "
                "the remote. Check that the network interface is up and the host has an IPv4 address."
            )

        try:
            zconf = Zeroconf()
            stack.callback(zconf.close)
            unpublish = await advertise_companion(
                loop, zconf, local_ip, bound_port,
                device_name=config.companion.device_name,
                model=config.companion.model,
            )
        except OSError as exc:
            # Without mDNS the phone can't find us at all, so fail with guidance instead of a raw trace.
            raise RuntimeError(
                f"mDNS advertisement failed ({exc}). Check that UDP 5353 isn't blocked by a local "
                "firewall, that the interface allows multicast, and (on segmented VLANs) that an mDNS "
                "reflector forwards _companion-link._tcp from the phone's network to this host."
            ) from exc
        stack.push_async_callback(unpublish)

        # The Apple side is now up, so the iPhone can discover + pair even if the TV is offline.
        # Surface what the operator should look for (never the PIN).
        _LOGGER.info(
            "Advertising %r on Companion port %s — pair from iPhone Control Center → Apple TV "
            "Remote and enter your PIN. The Samsung 'Allow' prompt appears as %r. State dir: %s",
            config.companion.device_name, bound_port, config.samsung.name,
            config.companion.state_dir or "(ephemeral — set companion.state_dir to persist pairing)",
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
                config.samsung.host, config.samsung.port, exc,
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


def _random_pin() -> str:
    """A non-weak 4-digit pairing PIN so a fresh `init` doesn't ship the example's guessable value."""
    while True:
        pin = f"{secrets.randbelow(10000):04d}"
        if not pin_is_weak(pin):
            return pin


def _cmd_init(path: str) -> int:
    import importlib.resources as ir

    dest = pathlib.Path(path).expanduser()
    if dest.exists():
        print(f"{dest} already exists — leaving it untouched.")
        return 0
    try:
        template = ir.files("atvr4samsung").joinpath("config.example.yaml").read_text()
    except (FileNotFoundError, ModuleNotFoundError):
        template = "companion:\n  device_name: \"Frame Living Room\"\n  pin: \"1337\"\nsamsung:\n  host: \"\"\n  mac: \"\"\n"

    pin = _random_pin()
    template, replaced = re.subn(r'(?m)^(\s*pin:\s*)".*"', rf'\g<1>"{pin}"', template, count=1)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(template)
    print(f"Wrote {dest}.")
    if replaced:
        print(f"Generated a random pairing PIN: {pin} (enter this on the iPhone when pairing).")
    print("Next: set samsung.host/mac, run `atvr4samsung --check`, then `doctor`.")
    return 0


def _cmd_install_service(config_path: str, apply: bool = False) -> int:
    exe = shutil.which("atvr4samsung") or f"{sys.executable} -m atvr4samsung.app"
    cfg = pathlib.Path(config_path).expanduser().resolve()
    user = os.environ.get("USER", "root")
    unit = f"""[Unit]
Description=atvr4samsung (emulated Apple TV -> Samsung Frame TV)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
ExecStart={exe} --config {cfg}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
LockPersonality=true

[Install]
WantedBy=multi-user.target
"""
    if apply:
        import subprocess
        try:
            subprocess.run(["sudo", "tee", "/etc/systemd/system/atvr4samsung.service"],
                           input=unit, text=True, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
            subprocess.run(["sudo", "systemctl", "enable", "--now", "atvr4samsung"], check=True)
            print("Installed + started atvr4samsung.service. Logs: journalctl -u atvr4samsung -f")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"error: could not install service ({exc}); re-run with sudo available.")
            return 1
        return 0
    print("Install with:\n")
    print(f"  sudo tee /etc/systemd/system/atvr4samsung.service >/dev/null <<'EOF'\n{unit}EOF")
    print("  sudo systemctl daemon-reload && sudo systemctl enable --now atvr4samsung")
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
    probe = target / ".doctor-write-test"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
        return True, f"{target} is writable"
    except OSError as exc:
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
    from .samsung.client import SamsungFrameClient

    client = SamsungFrameClient(
        host=config.samsung.host, mac=config.samsung.mac, port=config.samsung.port,
        name=config.samsung.name, token_file=config.samsung.token_file, connect_timeout=5.0,
    )
    try:
        await client.connect()
        return True, f"connected to {config.samsung.host}:{config.samsung.port}"
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        return False, f"not reachable now ({reason}) — OK if the TV is asleep; it wakes on first command"
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

    weak = pin_is_weak(config.companion.pin)
    add("Pairing PIN", not weak,
        "weak/guessable — set a stronger companion.pin" if weak else "looks strong", warn_only=True)

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
        add("State dir", True, "not set — pairing won't survive a restart", warn_only=True)

    if config.samsung.token_file:
        add("Samsung token path", *_probe_writable_dir(pathlib.Path(config.samsung.token_file).parent))

    add("mDNS (Zeroconf)", *await asyncio.get_event_loop().run_in_executor(None, _probe_zeroconf))
    add("Samsung TV reachable", *(await _probe_tv(config)), warn_only=True)

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


def _cmd_unpair(config: Config, *, reset_identity_too: bool = False) -> int:
    from .companion.protocol.paired_clients import PairedClients
    from .companion.protocol.server_identity import reset_identity

    state_dir = config.companion.state_dir
    if state_dir is None:
        print("No companion.state_dir configured — pairing is ephemeral, nothing persisted to clear.")
        return 0

    if PairedClients.clear_state(state_dir / "paired-clients.json"):
        print("Cleared paired iPhone(s).")
    else:
        print("No paired clients on file.")

    if reset_identity_too:
        if reset_identity(state_dir):
            print("Regenerated server identity — the bridge now looks like a brand-new Apple TV.")
        else:
            print("No server identity on file to reset.")

    print("On the iPhone: open the Apple TV Remote, remove this remote (\"Forget This Remote\"), then "
          "pair again with your PIN. The Samsung token was left untouched.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="atvr4samsung", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help=f"path to config.yaml (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--check", action="store_true",
                        help="validate config and print resolved settings, then exit (no network)")
    parser.add_argument("--apply", action="store_true",
                        help="with install-service: actually write + enable the unit (uses sudo)")
    parser.add_argument("--reset-identity", action="store_true",
                        help="with unpair: also regenerate the server identity (the iPhone must "
                             "'Forget This Remote' and re-pair)")
    parser.add_argument("command", nargs="?",
                        choices=["run", "init", "install-service", "doctor", "unpair"],
                        default="run",
                        help="run (default), init config, print a systemd unit, doctor "
                             "(network preflight), or unpair (clear paired iPhones)")
    args = parser.parse_args()

    if args.command == "init":
        return _cmd_init(args.config)
    if args.command == "install-service":
        return _cmd_install_service(args.config, apply=args.apply)

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
        _print_check(config)
        return 0

    if args.command == "doctor":
        return asyncio.run(_cmd_doctor(config))
    if args.command == "unpair":
        return _cmd_unpair(config, reset_identity_too=args.reset_identity)

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # don't dump a raw traceback at an operator; systemd sees the non-zero exit
        _LOGGER.error("Bridge stopped with an error: %s", exc)
        _LOGGER.debug("Fatal error detail", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

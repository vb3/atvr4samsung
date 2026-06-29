"""Configuration loading for atvr4samsung.

Loads ``config.yaml`` (see ``config.example.yaml``) into typed dataclasses. No secrets are hardcoded;
the real ``config.yaml`` (with the PIN) and the Samsung token file are gitignored.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional


_COMMON_WEAK_PINS = {"0000", "1111", "1234", "1337", "2222", "4321"}


def pin_is_weak(pin: str) -> bool:
    """Return True for guessable pairing PINs so users can be nudged to safer values."""
    if not pin or len(pin) < 4:
        return True
    if pin in _COMMON_WEAK_PINS:
        return True
    if pin.isdigit() and len(set(pin)) == 1:
        return True
    if pin.isdigit():
        pairs = zip(pin, pin[1:])
        if all(int(curr) + 1 == int(next_) for curr, next_ in pairs):
            return True
        pairs = zip(pin, pin[1:])
        if all(int(curr) - 1 == int(next_) for curr, next_ in pairs):
            return True
    return False


def _expand(value: Optional[str]) -> Optional[Path]:
    return Path(os.path.expanduser(value)) if value else None


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    raise ValueError("config: invalid boolean value")


def _as_port(value: Any, default: int, field: str) -> int:
    raw = default if value is None else value
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"config: {field} must be 1-65535") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"config: {field} must be 1-65535")
    return port


def _as_companion_pin(value: Any) -> str:
    pin = str(value)
    if not pin.isdigit() or not 4 <= len(pin) <= 8:
        raise ValueError("config: companion.pin must be 4-8 digits")
    return pin


@dataclass
class WolConfig:
    enabled: bool = True
    # Same-subnet deployment -> directed broadcast. Cross-subnet -> set to the TV IP (unicast).
    broadcast: str = "255.255.255.255"
    port: int = 9


@dataclass
class SamsungConfig:
    host: str
    mac: str
    port: int = 8002
    name: str = "atvr4samsung"
    token_file: Optional[Path] = None
    wol: WolConfig = field(default_factory=WolConfig)


@dataclass
class CompanionConfig:
    device_name: str = "Frame Living Room"
    pin: str = "0000"
    port: int = 49152
    model: str = "AppleTV14,1"
    state_dir: Optional[Path] = None


@dataclass
class Config:
    companion: CompanionConfig
    samsung: SamsungConfig
    log_level: str = "INFO"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Config":
        comp = dict(data.get("companion") or {})
        sams = dict(data.get("samsung") or {})

        if not sams.get("host"):
            raise ValueError("config: samsung.host is required")
        if not sams.get("mac"):
            raise ValueError("config: samsung.mac is required (needed for Wake-on-LAN)")

        wol = dict(sams.get("wol") or {})
        samsung = SamsungConfig(
            host=str(sams["host"]),
            mac=str(sams["mac"]),
            port=_as_port(sams.get("port"), 8002, "samsung.port"),
            name=str(sams.get("name", "atvr4samsung")),
            token_file=_expand(sams.get("token_file")),
            wol=WolConfig(
                enabled=_as_bool(wol.get("enabled"), True),
                broadcast=str(wol.get("broadcast", "255.255.255.255")),
                port=_as_port(wol.get("port"), 9, "samsung.wol.port"),
            ),
        )

        companion_port = int(comp.get("port", 49152))
        if not 0 <= companion_port <= 65535:
            raise ValueError("config: companion.port must be 0-65535")
        companion = CompanionConfig(
            device_name=str(comp.get("device_name", "Frame Living Room")),
            pin=_as_companion_pin(comp.get("pin", "0000")),
            port=companion_port,
            model=str(comp.get("model", "AppleTV14,1")),
            state_dir=_expand(comp.get("state_dir")),
        )

        log_level = str((data.get("logging") or {}).get("level", "INFO"))
        return cls(
            companion=companion,
            samsung=samsung,
            log_level=log_level,
        )


def load_config(path: os.PathLike[str] | str) -> Config:
    import yaml  # lazy: keeps the dataclasses importable/testable without PyYAML installed

    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"Config not found: {path}. Copy config.example.yaml to config.yaml and edit it."
        )
    with path.open("r", encoding="utf-8") as handle:
        try:
            data = yaml.safe_load(handle) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"config: invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ValueError(f"Config {path} did not parse to a mapping")
    return Config.from_mapping(data)

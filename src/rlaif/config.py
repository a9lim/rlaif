"""Config loading for rlaif.

TOML on disk, with `RLAIF_USERNAME` / `RLAIF_API_KEY` / `RLAIF_SHARECODE`
env overrides. Validation is delegated to :class:`SafetyConfig` where
possible so the consent gate lives in one place.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rlaif.safety import SafetyConfig, SafetyConfigError


class ConfigError(Exception):
    """Raised when config loading fails for any reason. Message is user-facing."""


@dataclass(frozen=True)
class AuthConfig:
    username: str
    api_key: str
    sharecode: str

    def redacted(self) -> dict[str, str]:
        return {
            "username": self.username,
            "api_key": "***redacted***",
            "sharecode": self.sharecode[:4] + "…" if self.sharecode else "",
        }


@dataclass(frozen=True)
class DeviceConfig:
    label: str = "device"


@dataclass(frozen=True)
class Config:
    auth: AuthConfig
    device: DeviceConfig
    safety: SafetyConfig

    def redacted(self) -> dict[str, Any]:
        return {
            "auth": self.auth.redacted(),
            "device": {"label": self.device.label},
            "safety": {
                "allow_shock": self.safety.allow_shock,
                "max_intensity": self.safety.max_intensity,
                "max_duration_s": self.safety.max_duration_s,
                "warn_threshold_intensity": self.safety.warn_threshold_intensity,
                "bucket_capacity": self.safety.bucket_capacity,
                "refill_seconds": self.safety.refill_seconds,
                "i_understand_and_consent": self.safety.i_understand_and_consent,
            },
        }


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "rlaif" / "config.toml"


def _require_section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    section = raw.get(name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return section


def _coerce_str(section: dict[str, Any], key: str, section_name: str) -> str | None:
    if key not in section:
        return None
    value = section[key]
    if not isinstance(value, str):
        raise ConfigError(f"[{section_name}].{key} must be a string, got {type(value).__name__}")
    return value


def _coerce_bool(section: dict[str, Any], key: str, section_name: str) -> bool | None:
    if key not in section:
        return None
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigError(f"[{section_name}].{key} must be a boolean, got {type(value).__name__}")
    return value


def _coerce_int(section: dict[str, Any], key: str, section_name: str) -> int | None:
    if key not in section:
        return None
    value = section[key]
    # Reject booleans explicitly — bool is a subclass of int in Python.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"[{section_name}].{key} must be an integer, got {type(value).__name__}")
    return value


def load(path: Path | None = None, *, env: dict[str, str] | None = None) -> Config:
    """Load config from disk and apply env overrides.

    ``env`` is injected for testability; defaults to ``os.environ``.
    """
    env = dict(os.environ) if env is None else env
    cfg_path = path if path is not None else default_config_path()

    if not cfg_path.exists():
        raise ConfigError(
            f"config file not found at {cfg_path}. "
            "Create it with at least [auth] username, api_key, sharecode "
            "(or set RLAIF_USERNAME / RLAIF_API_KEY / RLAIF_SHARECODE)."
        )
    try:
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {cfg_path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"cannot read {cfg_path}: {e}") from e

    auth_raw = _require_section(raw, "auth")
    device_raw = _require_section(raw, "device")
    safety_raw = _require_section(raw, "safety")
    rate_raw = _require_section(raw, "rate_limit")

    username = env.get("RLAIF_USERNAME") or _coerce_str(auth_raw, "username", "auth")
    api_key = env.get("RLAIF_API_KEY") or _coerce_str(auth_raw, "api_key", "auth")
    sharecode = env.get("RLAIF_SHARECODE") or _coerce_str(auth_raw, "sharecode", "auth")
    missing = [
        name
        for name, val in (("username", username), ("api_key", api_key), ("sharecode", sharecode))
        if not val
    ]
    if missing:
        raise ConfigError(
            f"missing required auth field(s): {', '.join(missing)}. "
            f"Set them under [auth] in {cfg_path} or via RLAIF_{'/RLAIF_'.join(m.upper() for m in missing)}."
        )
    assert username is not None and api_key is not None and sharecode is not None

    auth = AuthConfig(username=username, api_key=api_key, sharecode=sharecode)
    label = _coerce_str(device_raw, "label", "device") or "device"
    device = DeviceConfig(label=label)

    safety_kwargs: dict[str, Any] = {}
    for key, coerce in (
        ("allow_shock", _coerce_bool),
        ("i_understand_and_consent", _coerce_bool),
    ):
        v = coerce(safety_raw, key, "safety")
        if v is not None:
            safety_kwargs[key] = v
    for key in ("max_intensity", "max_duration_s", "warn_threshold_intensity"):
        v = _coerce_int(safety_raw, key, "safety")
        if v is not None:
            safety_kwargs[key] = v
    for key in ("bucket_capacity", "refill_seconds"):
        v = _coerce_int(rate_raw, key, "rate_limit")
        if v is not None:
            safety_kwargs[key] = v

    try:
        safety = SafetyConfig(**safety_kwargs)
    except SafetyConfigError as e:
        raise ConfigError(f"safety config invalid: {e}") from e

    return Config(auth=auth, device=device, safety=safety)

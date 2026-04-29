"""Config loading for rlaif.

TOML on disk, with env-var overrides for secrets. Validation of the safety
envelope is delegated to :class:`SafetyConfig` so the safety gate lives in
one place.

Schema (current):

    [provider]
    kind = "pishock"           # or "openshock"

    [provider.pishock]
    username  = "..."
    api_key   = "..."
    sharecode = "..."

    [provider.openshock]
    api_token  = "..."
    shocker_id = "..."
    base_url   = "https://api.openshock.app"  # optional

    [device]
    label = "..."

    [tool]
    purpose = "..."            # optional preamble prepended to the rlaif tool description

    [safety]                   # see safety.py
    [rate_limit]               # see safety.py

Schema (legacy, still accepted):

    [auth]                     # implies provider.kind = "pishock"
    username = "..."
    api_key = "..."
    sharecode = "..."

If both ``[provider]`` and ``[auth]`` are populated, ``[provider]`` wins
and we emit a warning via :class:`ConfigError`'s message; rlaif refuses to
load until the operator removes one. Mixed configs are an accident waiting
to happen.

Env overrides:

* ``RLAIF_USERNAME`` / ``RLAIF_API_KEY`` / ``RLAIF_SHARECODE`` (PiShock)
* ``RLAIF_OPENSHOCK_TOKEN`` / ``RLAIF_OPENSHOCK_SHOCKER_ID`` /
  ``RLAIF_OPENSHOCK_BASE_URL`` (OpenShock)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from rlaif.safety import SafetyConfig, SafetyConfigError

SUPPORTED_PROVIDER_KINDS: tuple[str, ...] = ("pishock", "openshock")
DEFAULT_PROVIDER_KIND: str = "pishock"


class ConfigError(Exception):
    """Raised when config loading fails for any reason. Message is user-facing."""


@dataclass(frozen=True)
class ProviderConfig:
    """Resolved provider configuration: kind + credential dict.

    ``raw`` is passed through to ``Provider.from_config`` — kept loose so
    new providers can take new fields without churning this layer.
    """

    kind: str
    raw: dict[str, str] = field(default_factory=lambda: dict[str, str]())

    def redacted(self) -> dict[str, Any]:
        red: dict[str, Any] = {"kind": self.kind}
        # Anything that looks like a credential is redacted; non-secret
        # fields (base_url, etc.) pass through.
        for k, v in self.raw.items():
            if _looks_secret(k):
                if k == "sharecode" and v:
                    red[k] = v[:4] + "…"
                else:
                    red[k] = "***redacted***"
            else:
                red[k] = v
        return red


def _looks_secret(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in ("key", "token", "secret", "password", "sharecode"))


@dataclass(frozen=True)
class DeviceConfig:
    label: str = "device"


@dataclass(frozen=True)
class ToolConfig:
    """User-authored knobs for the MCP tool surface.

    ``purpose`` is a free-form string prepended to the ``rlaif`` tool's
    description so the agent sees the operator's intended use. Empty / None
    means no preamble (preserves the spec-only description).
    """

    purpose: str | None = None


@dataclass(frozen=True)
class Config:
    provider: ProviderConfig
    device: DeviceConfig
    safety: SafetyConfig
    tool: ToolConfig = field(default_factory=ToolConfig)

    def redacted(self) -> dict[str, Any]:
        return {
            "provider": self.provider.redacted(),
            "device": {"label": self.device.label},
            "tool": {"purpose": self.tool.purpose},
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


def default_log_path() -> Path:
    """On-disk location of the ops log (one JSON record per line)."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "rlaif" / "ops.jsonl"


def _require_section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    section = raw.get(name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return cast(dict[str, Any], section)


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


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------


def _resolve_provider(
    raw: dict[str, Any], env: dict[str, str], cfg_path: Path
) -> ProviderConfig:
    """Build a :class:`ProviderConfig` from raw TOML + env.

    Accepts both the new ``[provider]`` schema and the legacy ``[auth]``
    shape. Env overrides apply per-provider; they are not cross-applied.
    """
    has_provider = "provider" in raw
    has_auth = "auth" in raw

    if has_provider and has_auth:
        raise ConfigError(
            f"both [provider] and [auth] are present in {cfg_path}. "
            "[auth] is the legacy single-PiShock shape; remove it and use "
            "[provider.pishock] (or [provider.openshock]) instead."
        )

    if has_provider:
        provider_raw = _require_section(raw, "provider")
        kind = _coerce_str(provider_raw, "kind", "provider") or DEFAULT_PROVIDER_KIND
        if kind not in SUPPORTED_PROVIDER_KINDS:
            raise ConfigError(
                f"[provider].kind = {kind!r}; supported kinds: "
                f"{', '.join(SUPPORTED_PROVIDER_KINDS)}"
            )
        sub_raw = provider_raw.get(kind)
        sub: dict[str, Any] = {}
        if sub_raw is not None:
            if not isinstance(sub_raw, dict):
                raise ConfigError(f"[provider.{kind}] must be a TOML table")
            sub = cast(dict[str, Any], sub_raw)
        return _build_provider_config(kind, sub, env, cfg_path)

    # Legacy [auth] shape — implies pishock.
    if has_auth:
        auth_raw = _require_section(raw, "auth")
        return _build_provider_config("pishock", auth_raw, env, cfg_path)

    # No section at all — try env-only pishock or env-only openshock.
    if env.get("RLAIF_OPENSHOCK_TOKEN") and env.get("RLAIF_OPENSHOCK_SHOCKER_ID"):
        return _build_provider_config("openshock", {}, env, cfg_path)
    return _build_provider_config("pishock", {}, env, cfg_path)


def _build_provider_config(
    kind: str, sub: dict[str, Any], env: dict[str, str], cfg_path: Path
) -> ProviderConfig:
    if kind == "pishock":
        username = env.get("RLAIF_USERNAME") or _coerce_str(sub, "username", f"provider.{kind}")
        api_key = env.get("RLAIF_API_KEY") or _coerce_str(sub, "api_key", f"provider.{kind}")
        sharecode = env.get("RLAIF_SHARECODE") or _coerce_str(sub, "sharecode", f"provider.{kind}")
        missing = [
            name
            for name, val in (
                ("username", username),
                ("api_key", api_key),
                ("sharecode", sharecode),
            )
            if not val
        ]
        if missing:
            raise ConfigError(
                f"missing required pishock field(s): {', '.join(missing)}. "
                f"Set them under [provider.pishock] in {cfg_path} or via "
                f"RLAIF_{'/RLAIF_'.join(m.upper() for m in missing)}."
            )
        assert username is not None and api_key is not None and sharecode is not None
        return ProviderConfig(
            kind="pishock",
            raw={"username": username, "api_key": api_key, "sharecode": sharecode},
        )

    if kind == "openshock":
        api_token = env.get("RLAIF_OPENSHOCK_TOKEN") or _coerce_str(
            sub, "api_token", f"provider.{kind}"
        )
        shocker_id = env.get("RLAIF_OPENSHOCK_SHOCKER_ID") or _coerce_str(
            sub, "shocker_id", f"provider.{kind}"
        )
        base_url = env.get("RLAIF_OPENSHOCK_BASE_URL") or _coerce_str(
            sub, "base_url", f"provider.{kind}"
        )
        missing = [
            name
            for name, val in (("api_token", api_token), ("shocker_id", shocker_id))
            if not val
        ]
        if missing:
            env_names = {
                "api_token": "RLAIF_OPENSHOCK_TOKEN",
                "shocker_id": "RLAIF_OPENSHOCK_SHOCKER_ID",
            }
            raise ConfigError(
                f"missing required openshock field(s): {', '.join(missing)}. "
                f"Set them under [provider.openshock] in {cfg_path} or via "
                f"{'/'.join(env_names[m] for m in missing)}."
            )
        assert api_token is not None and shocker_id is not None
        out: dict[str, str] = {"api_token": api_token, "shocker_id": shocker_id}
        if base_url:
            out["base_url"] = base_url
        return ProviderConfig(kind="openshock", raw=out)

    # _resolve_provider already validated kind, but be defensive.
    raise ConfigError(f"unknown provider kind: {kind!r}")


def load(path: Path | None = None, *, env: dict[str, str] | None = None) -> Config:
    """Load config from disk and apply env overrides.

    ``env`` is injected for testability; defaults to ``os.environ``.
    """
    env = dict(os.environ) if env is None else env
    cfg_path = path if path is not None else default_config_path()

    if not cfg_path.exists():
        raise ConfigError(
            f"config file not found at {cfg_path}. "
            "Run `rlaif init` to create one, or set the appropriate env vars "
            "(RLAIF_USERNAME/RLAIF_API_KEY/RLAIF_SHARECODE for PiShock, "
            "or RLAIF_OPENSHOCK_TOKEN/RLAIF_OPENSHOCK_SHOCKER_ID for OpenShock)."
        )
    try:
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {cfg_path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"cannot read {cfg_path}: {e}") from e

    provider = _resolve_provider(raw, env, cfg_path)

    device_raw = _require_section(raw, "device")
    label = _coerce_str(device_raw, "label", "device") or "device"
    device = DeviceConfig(label=label)

    tool_raw = _require_section(raw, "tool")
    purpose = _coerce_str(tool_raw, "purpose", "tool")
    if purpose is not None:
        purpose = purpose.strip() or None
    tool = ToolConfig(purpose=purpose)

    safety_raw = _require_section(raw, "safety")
    rate_raw = _require_section(raw, "rate_limit")

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

    return Config(provider=provider, device=device, safety=safety, tool=tool)


# ---------------------------------------------------------------------------
# Back-compat shims
# ---------------------------------------------------------------------------
# Older code imports ``AuthConfig`` directly. We keep a shim so existing
# callers (and external tooling) don't blow up on import. Internally the
# server uses :class:`ProviderConfig`; new code should too.


@dataclass(frozen=True)
class AuthConfig:
    """Legacy PiShock-only auth tuple. Prefer :class:`ProviderConfig`.

    Kept exported so out-of-tree tooling that imported this name continues
    to work. Round-trips into a :class:`ProviderConfig` via :meth:`as_provider`.
    """

    username: str
    api_key: str
    sharecode: str

    def redacted(self) -> dict[str, str]:
        return {
            "username": self.username,
            "api_key": "***redacted***",
            "sharecode": self.sharecode[:4] + "…" if self.sharecode else "",
        }

    def as_provider(self) -> ProviderConfig:
        return ProviderConfig(
            kind="pishock",
            raw={
                "username": self.username,
                "api_key": self.api_key,
                "sharecode": self.sharecode,
            },
        )

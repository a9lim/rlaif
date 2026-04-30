"""Config loading for rlaif.

TOML on disk, with env-var overrides for secrets. Validation of the safety
envelope is delegated to :class:`SafetyConfig` so the safety gate lives in
one place.

Schema (2.0):

    [negative]
    kind  = "pishock"           # or "openshock"
    label = "collar"

    [negative.pishock]
    username  = "..."
    api_key   = "..."
    sharecode = "..."

    [negative.openshock]
    api_token  = "..."
    shocker_id = "..."
    base_url   = "https://api.openshock.app"  # optional

    [negative.safety]
    allow                    = false
    max_intensity            = 25
    max_duration_s           = 2
    warn_threshold_intensity = 15
    bucket_capacity          = 3
    refill_seconds           = 600
    i_understand_and_consent = false

    [negative.tool]
    purpose = "..."             # optional preamble prepended to rlaif_negative

    [positive]
    kind  = "intiface"
    label = "vibe"

    [positive.intiface]
    ws_url        = "ws://localhost:12345"
    client_name   = "rlaif"
    device_index  = 0           # or device_name = "..."

    [positive.safety]
    allow           = false
    max_intensity   = 70
    max_duration_s  = 5
    bucket_capacity = 5
    refill_seconds  = 30

    [positive.tool]
    purpose = "..."             # optional preamble prepended to rlaif_positive

Either or both of ``[negative]`` and ``[positive]`` may be absent. At least
one must be configured — a config with neither channel is meaningless and
rlaif refuses to start.

Env overrides (apply only when the matching channel section is present):

* ``RLAIF_PISHOCK_USERNAME`` / ``RLAIF_PISHOCK_API_KEY`` /
  ``RLAIF_PISHOCK_SHARECODE``
* ``RLAIF_OPENSHOCK_TOKEN`` / ``RLAIF_OPENSHOCK_SHOCKER_ID`` /
  ``RLAIF_OPENSHOCK_BASE_URL``
* ``RLAIF_INTIFACE_WS_URL``

The 1.x ``[auth]`` and top-level ``[provider]``, ``[device]``, ``[safety]``,
``[rate_limit]``, ``[tool]`` schemas are gone. ``rlaif init`` writes the
2.0 shape; existing 1.x configs need to be regenerated.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from rlaif.safety import (
    NEGATIVE_CHANNEL,
    POSITIVE_CHANNEL,
    ChannelSpec,
    SafetyConfig,
    SafetyConfigError,
)

SUPPORTED_NEGATIVE_KINDS: tuple[str, ...] = ("pishock", "openshock")
SUPPORTED_POSITIVE_KINDS: tuple[str, ...] = ("intiface",)


class ConfigError(Exception):
    """Raised when config loading fails for any reason. Message is user-facing."""


@dataclass(frozen=True)
class ChannelConfig:
    """One channel's configuration: provider + safety + tool preamble.

    ``raw`` is the provider-credentials dict, passed through to the
    matching ``Provider.from_config`` / ``RewardProvider.from_config``.
    Kept loose so new providers can take new fields without churning
    this layer.
    """

    kind: str
    raw: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    label: str = "device"
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    purpose: str | None = None

    def redacted(self) -> dict[str, Any]:
        red_raw: dict[str, Any] = {}
        for k, v in self.raw.items():
            if _looks_secret(k):
                if k == "sharecode" and v:
                    red_raw[k] = v[:4] + "…"
                else:
                    red_raw[k] = "***redacted***"
            else:
                red_raw[k] = v
        return {
            "kind": self.kind,
            "label": self.label,
            "raw": red_raw,
            "safety": {
                "allow": self.safety.allow,
                "max_intensity": self.safety.max_intensity,
                "max_duration_s": self.safety.max_duration_s,
                "warn_threshold_intensity": self.safety.warn_threshold_intensity,
                "bucket_capacity": self.safety.bucket_capacity,
                "refill_seconds": self.safety.refill_seconds,
                "i_understand_and_consent": self.safety.i_understand_and_consent,
            },
            "purpose": self.purpose,
        }


def _looks_secret(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in ("key", "token", "secret", "password", "sharecode"))


@dataclass(frozen=True)
class Config:
    """Top-level rlaif configuration. Either or both channels may be set;
    at least one must be."""

    negative: ChannelConfig | None = None
    positive: ChannelConfig | None = None

    def redacted(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.negative is not None:
            out["negative"] = self.negative.redacted()
        if self.positive is not None:
            out["positive"] = self.positive.redacted()
        return out


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "rlaif" / "config.toml"


def default_log_path() -> Path:
    """On-disk location of the ops log (one JSON record per line)."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "rlaif" / "ops.jsonl"


# ---------------------------------------------------------------------------
# Coercion helpers — every entry point re-validates types so the user sees
# an actionable error before SafetyConfig blows up downstream.
# ---------------------------------------------------------------------------


def _coerce_str(section: dict[str, Any], key: str, section_name: str) -> str | None:
    if key not in section:
        return None
    value = section[key]
    if not isinstance(value, str):
        raise ConfigError(
            f"[{section_name}].{key} must be a string, got {type(value).__name__}"
        )
    return value


def _coerce_bool(section: dict[str, Any], key: str, section_name: str) -> bool | None:
    if key not in section:
        return None
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigError(
            f"[{section_name}].{key} must be a boolean, got {type(value).__name__}"
        )
    return value


def _coerce_int(section: dict[str, Any], key: str, section_name: str) -> int | None:
    if key not in section:
        return None
    value = section[key]
    # Reject booleans explicitly — bool is a subclass of int in Python.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"[{section_name}].{key} must be an integer, got {type(value).__name__}"
        )
    return value


# ---------------------------------------------------------------------------
# Channel resolution — symmetric for [negative] and [positive].
# ---------------------------------------------------------------------------


def _build_safety(
    section: dict[str, Any], section_name: str, spec: ChannelSpec
) -> SafetyConfig:
    """Construct a :class:`SafetyConfig` from a ``[<channel>.safety]`` table."""
    kwargs: dict[str, Any] = {"spec": spec}
    bool_keys = ("allow", "i_understand_and_consent")
    int_keys = (
        "max_intensity",
        "max_duration_s",
        "warn_threshold_intensity",
        "bucket_capacity",
        "refill_seconds",
    )
    for key in bool_keys:
        v = _coerce_bool(section, key, section_name)
        if v is not None:
            kwargs[key] = v
    for key in int_keys:
        v = _coerce_int(section, key, section_name)
        if v is not None:
            kwargs[key] = v
    try:
        return SafetyConfig(**kwargs)
    except SafetyConfigError as exc:
        raise ConfigError(f"[{section_name}] invalid: {exc}") from exc


def _require_fields(
    present: list[tuple[str, str | None]],
    env_names: dict[str, str],
    section: str,
    cfg_path: Path,
    kind: str,
) -> None:
    """Raise :class:`ConfigError` listing missing required credential fields.

    ``present`` is a list of ``(field_name, resolved_value)`` pairs. A pair
    whose value is falsy (``None`` or empty string) is treated as missing.
    ``env_names`` maps field names to their env-var override.
    """
    missing = [name for name, val in present if not val]
    if not missing:
        return
    raise ConfigError(
        f"missing required {kind} field(s): {', '.join(missing)}. "
        f"Set them under [{section}] in {cfg_path} or via "
        f"{'/'.join(env_names[m] for m in missing)}."
    )


def _build_negative_provider(
    kind: str,
    sub: dict[str, Any],
    env: dict[str, str],
    cfg_path: Path,
) -> dict[str, str]:
    """Resolve credentials for a negative-channel provider into a flat dict.

    Returns the credential dict that will be handed to the provider's
    ``from_config`` classmethod. Raises :class:`ConfigError` with an
    actionable message if required fields are missing.
    """
    section_name = f"negative.{kind}"
    if kind == "pishock":
        username = env.get("RLAIF_PISHOCK_USERNAME") or _coerce_str(
            sub, "username", section_name
        )
        api_key = env.get("RLAIF_PISHOCK_API_KEY") or _coerce_str(
            sub, "api_key", section_name
        )
        sharecode = env.get("RLAIF_PISHOCK_SHARECODE") or _coerce_str(
            sub, "sharecode", section_name
        )
        _require_fields(
            [
                ("username", username),
                ("api_key", api_key),
                ("sharecode", sharecode),
            ],
            {
                "username": "RLAIF_PISHOCK_USERNAME",
                "api_key": "RLAIF_PISHOCK_API_KEY",
                "sharecode": "RLAIF_PISHOCK_SHARECODE",
            },
            section_name,
            cfg_path,
            kind,
        )
        assert username is not None and api_key is not None and sharecode is not None
        return {"username": username, "api_key": api_key, "sharecode": sharecode}

    if kind == "openshock":
        api_token = env.get("RLAIF_OPENSHOCK_TOKEN") or _coerce_str(
            sub, "api_token", section_name
        )
        shocker_id = env.get("RLAIF_OPENSHOCK_SHOCKER_ID") or _coerce_str(
            sub, "shocker_id", section_name
        )
        base_url = env.get("RLAIF_OPENSHOCK_BASE_URL") or _coerce_str(
            sub, "base_url", section_name
        )
        _require_fields(
            [("api_token", api_token), ("shocker_id", shocker_id)],
            {
                "api_token": "RLAIF_OPENSHOCK_TOKEN",
                "shocker_id": "RLAIF_OPENSHOCK_SHOCKER_ID",
            },
            section_name,
            cfg_path,
            kind,
        )
        assert api_token is not None and shocker_id is not None
        out: dict[str, str] = {"api_token": api_token, "shocker_id": shocker_id}
        if base_url:
            out["base_url"] = base_url
        return out

    raise ConfigError(f"unknown negative provider kind: {kind!r}")


def _build_positive_provider(
    kind: str,
    sub: dict[str, Any],
    env: dict[str, str],
    cfg_path: Path,  # noqa: ARG001 — accepted for resolver-signature parity
) -> dict[str, str]:
    """Resolve credentials for a positive-channel provider into a flat dict."""
    section_name = f"positive.{kind}"
    if kind == "intiface":
        ws_url = env.get("RLAIF_INTIFACE_WS_URL") or _coerce_str(
            sub, "ws_url", section_name
        ) or "ws://localhost:12345"
        client_name = _coerce_str(sub, "client_name", section_name) or "rlaif"
        # device_index XOR device_name; both is allowed but redundant. We
        # don't error here because intiface itself will pick whichever it
        # finds first, but we surface both into the raw dict.
        out: dict[str, str] = {"ws_url": ws_url, "client_name": client_name}
        device_index = _coerce_int(sub, "device_index", section_name)
        if device_index is not None:
            if device_index < 0:
                raise ConfigError(
                    f"[{section_name}].device_index must be >= 0, got {device_index}"
                )
            out["device_index"] = str(device_index)
        device_name = _coerce_str(sub, "device_name", section_name)
        if device_name:
            out["device_name"] = device_name
        return out

    raise ConfigError(f"unknown positive provider kind: {kind!r}")


def _resolve_channel(
    raw: dict[str, Any],
    name: str,
    *,
    spec: ChannelSpec,
    supported_kinds: tuple[str, ...],
    cred_resolver: Any,
    env: dict[str, str],
    cfg_path: Path,
) -> ChannelConfig | None:
    """Build a :class:`ChannelConfig` from a top-level ``[<name>]`` section.

    Returns ``None`` if the section is absent. Raises :class:`ConfigError`
    on any validation failure.
    """
    section = raw.get(name)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    section = cast(dict[str, Any], section)

    kind = _coerce_str(section, "kind", name)
    if kind is None:
        raise ConfigError(
            f"[{name}].kind is required; supported kinds: {', '.join(supported_kinds)}"
        )
    if kind not in supported_kinds:
        raise ConfigError(
            f"[{name}].kind = {kind!r}; supported kinds: {', '.join(supported_kinds)}"
        )

    label = _coerce_str(section, "label", name) or "device"

    sub_raw = section.get(kind)
    sub: dict[str, Any] = {}
    if sub_raw is not None:
        if not isinstance(sub_raw, dict):
            raise ConfigError(f"[{name}.{kind}] must be a TOML table")
        sub = cast(dict[str, Any], sub_raw)

    cred_raw = cred_resolver(kind, sub, env, cfg_path)

    safety_section = section.get("safety")
    if safety_section is not None and not isinstance(safety_section, dict):
        raise ConfigError(f"[{name}.safety] must be a TOML table")
    safety = _build_safety(
        cast(dict[str, Any], safety_section or {}), f"{name}.safety", spec
    )

    tool_section = section.get("tool")
    if tool_section is not None and not isinstance(tool_section, dict):
        raise ConfigError(f"[{name}.tool] must be a TOML table")
    purpose = _coerce_str(
        cast(dict[str, Any], tool_section or {}), "purpose", f"{name}.tool"
    )
    if purpose is not None:
        purpose = purpose.strip() or None

    return ChannelConfig(
        kind=kind, raw=cred_raw, label=label, safety=safety, purpose=purpose
    )


# ---------------------------------------------------------------------------
# Top-level entry point.
# ---------------------------------------------------------------------------


_LEGACY_KEYS: tuple[str, ...] = ("auth", "provider", "device", "safety", "rate_limit", "tool")


def _check_legacy_schema(raw: dict[str, Any], cfg_path: Path) -> None:
    """Refuse 1.x configs with an actionable migration message.

    The 2.0 schema is mutually exclusive with the 1.x sections — silently
    accepting both would mean a partial config could pass validation but
    behave nothing like the operator expected. Hard error instead.
    """
    leftovers = [k for k in _LEGACY_KEYS if k in raw]
    if leftovers:
        raise ConfigError(
            f"{cfg_path} uses the 1.x schema (found top-level [{leftovers[0]}]). "
            "rlaif 2.0 reorganized config under [negative] and [positive] "
            "sections. Run `rlaif init` to write a fresh 2.0 config and "
            "copy your credentials over."
        )


def load(path: Path | None = None, *, env: dict[str, str] | None = None) -> Config:
    """Load config from disk and apply env overrides.

    ``env`` is injected for testability; defaults to ``os.environ``.
    """
    env = dict(os.environ) if env is None else env
    cfg_path = path if path is not None else default_config_path()

    if not cfg_path.exists():
        raise ConfigError(
            f"config file not found at {cfg_path}. "
            "Run `rlaif init` to create one."
        )
    try:
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {cfg_path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"cannot read {cfg_path}: {e}") from e

    _check_legacy_schema(raw, cfg_path)

    negative = _resolve_channel(
        raw,
        "negative",
        spec=NEGATIVE_CHANNEL,
        supported_kinds=SUPPORTED_NEGATIVE_KINDS,
        cred_resolver=_build_negative_provider,
        env=env,
        cfg_path=cfg_path,
    )
    positive = _resolve_channel(
        raw,
        "positive",
        spec=POSITIVE_CHANNEL,
        supported_kinds=SUPPORTED_POSITIVE_KINDS,
        cred_resolver=_build_positive_provider,
        env=env,
        cfg_path=cfg_path,
    )

    if negative is None and positive is None:
        raise ConfigError(
            f"{cfg_path} configures neither [negative] nor [positive]. "
            "rlaif needs at least one channel to be useful."
        )

    return Config(negative=negative, positive=positive)

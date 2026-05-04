"""Config loading for rlaif.

TOML on disk, with env-var overrides for secrets. Validation of the safety
envelope is delegated to :class:`SafetyConfig` so the safety gate lives in
one place.

Schema (2.0):

    [negative]
    kind  = "pishock"           # or "openshock"
    label = "collar"

    [negative.pishock]
    username   = "..."
    api_token  = "..."          # the pishock.com "API key"
    shocker_id = "..."          # the per-device share code

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
    purpose = "..."             # optional preamble prepended to the `negative` tool

    [positive]
    kind  = "intiface"
    label = "vibe"

    [positive.intiface]
    base_url    = "ws://localhost:12345"
    device_name = "..."         # exact device name or display name

    [positive.safety]
    allow           = false
    max_intensity   = 75
    max_duration_s  = 5
    bucket_capacity = 5
    refill_seconds  = 30

    [positive.tool]
    purpose = "..."             # optional preamble prepended to the `positive` tool

Either or both of ``[negative]`` and ``[positive]`` may be absent. At least
one must be configured — a config with neither channel is meaningless and
rlaif refuses to start.

PiShock and OpenShock share the ``api_token`` / ``shocker_id`` field names —
PiShock's ``api_token`` is the value pishock.com calls the "API key", and its
``shocker_id`` is the per-device share code. Older configs that used
``api_key`` / ``sharecode`` are rejected with a one-line migration message.

Env overrides (apply only when the matching channel section is present):

* ``RLAIF_PISHOCK_USERNAME`` / ``RLAIF_PISHOCK_API_TOKEN`` /
  ``RLAIF_PISHOCK_SHOCKER_ID``
* ``RLAIF_OPENSHOCK_API_TOKEN`` / ``RLAIF_OPENSHOCK_SHOCKER_ID`` /
  ``RLAIF_OPENSHOCK_BASE_URL``
* ``RLAIF_INTIFACE_BASE_URL``

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
            red_raw[k] = _redact_field(self.kind, k, v)
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


def _redact_field(kind: str, key: str, value: str) -> str:
    """Per-provider redaction for one credential field.

    PiShock's ``shocker_id`` is the share code — capability-bearing on its
    own — so we prefix-redact it (first four chars + ellipsis) to give the
    operator a hint of which device without leaking the whole code into
    logs. OpenShock's ``shocker_id`` is a UUID that shows up in the
    dashboard and is not a secret on its own; leave it alone. Anything
    that smells like a key/token/password gets fully redacted regardless
    of provider.
    """
    if not value:
        return value
    k = key.lower()
    if any(s in k for s in ("key", "token", "secret", "password")):
        return "***redacted***"
    if kind == "pishock" and key == "shocker_id":
        return value[:4] + "…"
    return value


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
# Channel resolution — symmetric for [negative] and [positive].
# ---------------------------------------------------------------------------


def _build_safety(section: dict[str, Any], section_name: str, spec: ChannelSpec) -> SafetyConfig:
    """Construct a :class:`SafetyConfig` from a ``[<channel>.safety]`` table.

    Missing keys fall back to the channel's per-spec defaults
    (``ChannelSpec.default_*``), which is what makes a partial
    ``[positive.safety]`` block end up with positive defaults rather than
    inheriting the negative ones from the dataclass-level field defaults.
    """
    overrides: dict[str, Any] = {}
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
            overrides[key] = v
    for key in int_keys:
        v = _coerce_int(section, key, section_name)
        if v is not None:
            overrides[key] = v
    try:
        return SafetyConfig.for_spec(spec, **overrides)
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
        _reject_legacy_pishock_keys(sub, section_name)
        username = env.get("RLAIF_PISHOCK_USERNAME") or _coerce_str(sub, "username", section_name)
        api_token = env.get("RLAIF_PISHOCK_API_TOKEN") or _coerce_str(sub, "api_token", section_name)
        shocker_id = env.get("RLAIF_PISHOCK_SHOCKER_ID") or _coerce_str(sub, "shocker_id", section_name)
        _require_fields(
            [
                ("username", username),
                ("api_token", api_token),
                ("shocker_id", shocker_id),
            ],
            {
                "username": "RLAIF_PISHOCK_USERNAME",
                "api_token": "RLAIF_PISHOCK_API_TOKEN",
                "shocker_id": "RLAIF_PISHOCK_SHOCKER_ID",
            },
            section_name,
            cfg_path,
            kind,
        )
        assert username is not None and api_token is not None and shocker_id is not None
        return {
            "username": username,
            "api_token": api_token,
            "shocker_id": shocker_id,
        }

    if kind == "openshock":
        api_token = env.get("RLAIF_OPENSHOCK_API_TOKEN") or _coerce_str(sub, "api_token", section_name)
        shocker_id = env.get("RLAIF_OPENSHOCK_SHOCKER_ID") or _coerce_str(sub, "shocker_id", section_name)
        base_url = env.get("RLAIF_OPENSHOCK_BASE_URL") or _coerce_str(sub, "base_url", section_name)
        _require_fields(
            [("api_token", api_token), ("shocker_id", shocker_id)],
            {
                "api_token": "RLAIF_OPENSHOCK_API_TOKEN",
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


def _reject_legacy_pishock_keys(sub: dict[str, Any], section_name: str) -> None:
    """Refuse the pre-2.0 PiShock field names with an actionable message.

    The 2.0 schema fuses PiShock's ``api_key``/``sharecode`` onto OpenShock's
    ``api_token``/``shocker_id`` so both backends share a vocabulary. A
    config that still uses the old names is almost certainly a stale 1.x
    or pre-fuse 2.0 file the operator forgot to regenerate; surface a hard
    error pointing at the new names rather than silently dropping the keys.
    """
    legacy = {"api_key": "api_token", "sharecode": "shocker_id"}
    found = [k for k in legacy if k in sub]
    if not found:
        return
    pairs = ", ".join(f"{old}→{legacy[old]}" for old in found)
    raise ConfigError(
        f"[{section_name}] uses pre-fuse field names ({pairs}). "
        f"PiShock now shares OpenShock's vocabulary: rename `api_key` to "
        f"`api_token` and `sharecode` to `shocker_id` (or run `rlaif init` "
        f"to regenerate the file)."
    )


def _build_positive_provider(
    kind: str,
    sub: dict[str, Any],
    env: dict[str, str],
    cfg_path: Path,  # noqa: ARG001 — accepted for resolver-signature parity
) -> dict[str, str]:
    """Resolve credentials for a positive-channel provider into a flat dict.

    Devices are selected by exact name (or display name) only; a stale
    ``device_index`` from a pre-2.0 config is rejected with a hint so
    operators can switch to ``device_name``. The Intiface client name is
    always ``rlaif``; we no longer take it from config.
    """
    section_name = f"positive.{kind}"
    if kind == "intiface":
        _reject_legacy_intiface_keys(sub, section_name)
        base_url = (
            env.get("RLAIF_INTIFACE_BASE_URL") or _coerce_str(sub, "base_url", section_name) or "ws://localhost:12345"
        )
        out: dict[str, str] = {"base_url": base_url}
        device_name = _coerce_str(sub, "device_name", section_name)
        if device_name:
            out["device_name"] = device_name
        return out

    raise ConfigError(f"unknown positive provider kind: {kind!r}")


def _reject_legacy_intiface_keys(sub: dict[str, Any], section_name: str) -> None:
    """Refuse pre-fuse ``[positive.intiface]`` keys with an actionable message.

    ``ws_url`` was renamed to ``base_url`` for parity with OpenShock's
    ``base_url``. ``client_name`` is hard-coded to ``rlaif`` and no longer
    user-configurable. ``device_index`` was dropped in favor of
    ``device_name`` (exact name or display name) — we never had a stable
    index ordering across reconnects, so an index lookup was always
    fragile.
    """
    if "ws_url" in sub:
        raise ConfigError(
            f"[{section_name}] uses the pre-fuse `ws_url` key. "
            f"Rename it to `base_url` (matches OpenShock's `base_url`)."
        )
    if "client_name" in sub:
        raise ConfigError(
            f"[{section_name}] sets `client_name`, which is no longer "
            f"configurable. The Intiface client name is always `rlaif`; "
            f"please remove the line."
        )
    if "device_index" in sub:
        raise ConfigError(
            f"[{section_name}] uses `device_index`, which has been removed. "
            f'Configure `device_name = "<exact device or display name>"` '
            f"instead — `rlaif doctor` prints visible device names."
        )


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
        raise ConfigError(f"[{name}].kind is required; supported kinds: {', '.join(supported_kinds)}")
    if kind not in supported_kinds:
        raise ConfigError(f"[{name}].kind = {kind!r}; supported kinds: {', '.join(supported_kinds)}")

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
    safety = _build_safety(cast(dict[str, Any], safety_section or {}), f"{name}.safety", spec)

    tool_section = section.get("tool")
    if tool_section is not None and not isinstance(tool_section, dict):
        raise ConfigError(f"[{name}.tool] must be a TOML table")
    purpose = _coerce_str(cast(dict[str, Any], tool_section or {}), "purpose", f"{name}.tool")
    if purpose is not None:
        purpose = purpose.strip() or None

    return ChannelConfig(kind=kind, raw=cred_raw, label=label, safety=safety, purpose=purpose)


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
        raise ConfigError(f"config file not found at {cfg_path}. Run `rlaif init` to create one.")
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
            f"{cfg_path} configures neither [negative] nor [positive]. rlaif needs at least one channel to be useful."
        )

    return Config(negative=negative, positive=positive)

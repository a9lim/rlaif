"""Safety core for rlaif.

Pure Python. Zero MCP imports. The server owns one :class:`SafetyState` per
channel (negative + optional positive) and calls into it from tool handlers.
Tests construct fresh states directly.

Two channels are first-class:

* ``negative``
* ``positive``

Per-channel constants (input ranges, code ceilings, consent thresholds) live
in :class:`ChannelSpec`; module-level :data:`NEGATIVE_CHANNEL` and
:data:`POSITIVE_CHANNEL` provide the presets. Same token-bucket math, same
ops-log shape, same authorize/commit/rollback flow — the spec just changes
the labels and ceilings.

Flow for either channel:

    rec = state.authorize(intensity, duration_s, now)
    if rec.rate_limited or rec.error is not None:
        return rec                          # already logged; do NOT fire
    try:
        resp = device_call(rec.actual[...])
    except Exception as exc:
        rec = state.rollback(rec, str(exc)) # refunds the token + logs
    else:
        rec = state.commit(rec, resp)       # logs success

``authorize`` clamps, runs the consent-gated caps, checks ``allow``, and
consumes a token from the bucket when granted. On refusal, no token is spent.
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import islice
from typing import Any

# Fraction of configured cap that triggers a `near_ceiling` warning on an op.
NEAR_CEILING_RATIO: float = 0.80

# Cap on the agent-supplied `reason` string. Long reasons cost log space
# without adding signal — clip on input.
REASON_MAX_LEN: int = 200

# Ops log retention.
OPS_LOG_CAPACITY: int = 200
OPS_LOG_DEFAULT_LIMIT: int = 10


# ---------------------------------------------------------------------------
# ChannelSpec — per-channel safety constants.
#
# The negative side and the positive side share their math (token bucket,
# clamping, ops log, rollback semantics) but differ in their ceilings,
# input ranges, consent thresholds, and labels. Encoding that in a single
# frozen value object keeps SafetyConfig / SafetyState completely
# channel-agnostic.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelSpec:
    """Per-channel safety constants and labels.

    ``config_path`` is the dotted operator-readable path of the allow flag
    in TOML (``negative.safety.allow`` / ``positive.safety.allow``). It
    lands in refusal messages so the operator can fix the offending key
    in their config without grepping for what's where.
    """

    name: str
    config_path: str
    intensity_input_min: int
    intensity_input_max: int
    duration_input_min_s: int
    duration_input_max_s: int
    intensity_code_ceiling: int
    duration_code_ceiling_s: int
    bucket_capacity_code_ceiling: int
    refill_seconds_code_floor: int
    intensity_consent_threshold: int
    bucket_capacity_consent_threshold: int


NEGATIVE_CHANNEL: ChannelSpec = ChannelSpec(
    name="negative",
    config_path="negative.safety.allow",
    intensity_input_min=1,
    intensity_input_max=100,
    duration_input_min_s=1,
    duration_input_max_s=15,
    intensity_code_ceiling=50,
    duration_code_ceiling_s=5,
    bucket_capacity_code_ceiling=10,
    refill_seconds_code_floor=60,
    intensity_consent_threshold=25,
    bucket_capacity_consent_threshold=3,
)

# The positive channel's ceilings are deliberately permissive. Vibration is
# not medically risky and the threat model is "agent spams reward signals
# during a runaway loop" rather than "agent harms the operator". The
# disconnect watchdog (provider-side) is what stops a stuck device, not
# this layer's caps. Consent thresholds equal the code ceilings, which
# means consent is effectively never required on the positive channel.
POSITIVE_CHANNEL: ChannelSpec = ChannelSpec(
    name="positive",
    config_path="positive.safety.allow",
    intensity_input_min=1,
    intensity_input_max=100,
    duration_input_min_s=1,
    duration_input_max_s=60,
    intensity_code_ceiling=100,
    duration_code_ceiling_s=30,
    bucket_capacity_code_ceiling=30,
    refill_seconds_code_floor=10,
    intensity_consent_threshold=100,
    bucket_capacity_consent_threshold=30,
)


# ---------------------------------------------------------------------------
# Convenience aliases for the negative channel's ceilings. Auditors and
# tests grep for these by name — keep them findable without forcing every
# read site to dotted-attribute through ChannelSpec.
# ---------------------------------------------------------------------------

INTENSITY_CODE_CEILING: int = NEGATIVE_CHANNEL.intensity_code_ceiling
DURATION_CODE_CEILING_S: int = NEGATIVE_CHANNEL.duration_code_ceiling_s
BUCKET_CAPACITY_CODE_CEILING: int = NEGATIVE_CHANNEL.bucket_capacity_code_ceiling
REFILL_SECONDS_CODE_FLOOR: int = NEGATIVE_CHANNEL.refill_seconds_code_floor
INTENSITY_CONSENT_THRESHOLD: int = NEGATIVE_CHANNEL.intensity_consent_threshold
BUCKET_CAPACITY_CONSENT_THRESHOLD: int = NEGATIVE_CHANNEL.bucket_capacity_consent_threshold
INTENSITY_INPUT_MIN: int = NEGATIVE_CHANNEL.intensity_input_min
INTENSITY_INPUT_MAX: int = NEGATIVE_CHANNEL.intensity_input_max
DURATION_INPUT_MIN_S: int = NEGATIVE_CHANNEL.duration_input_min_s
DURATION_INPUT_MAX_S: int = NEGATIVE_CHANNEL.duration_input_max_s


class SafetyConfigError(ValueError):
    """Raised when a :class:`SafetyConfig` fails validation."""


@dataclass(frozen=True)
class SafetyConfig:
    """Operator-authored safety envelope. Immutable once constructed.

    ``spec`` selects the channel; defaults to :data:`NEGATIVE_CHANNEL`
    so the common ``SafetyConfig(allow=True, ...)`` form means the
    negative channel without forcing every test to pass the spec.
    ``allow`` is the channel-agnostic gate; ``spec.config_path`` carries
    the operator-readable TOML path used in refusal messages.
    """

    spec: ChannelSpec = NEGATIVE_CHANNEL
    allow: bool = False
    max_intensity: int = 25
    max_duration_s: int = 2
    warn_threshold_intensity: int = 15
    bucket_capacity: int = 3
    refill_seconds: int = 600
    i_understand_and_consent: bool = False

    def __post_init__(self) -> None:
        spec = self.spec
        if not 1 <= self.max_intensity <= spec.intensity_code_ceiling:
            raise SafetyConfigError(
                f"max_intensity must be in [1, {spec.intensity_code_ceiling}], "
                f"got {self.max_intensity}"
            )
        if not 1 <= self.max_duration_s <= spec.duration_code_ceiling_s:
            raise SafetyConfigError(
                f"max_duration_s must be in [1, {spec.duration_code_ceiling_s}], "
                f"got {self.max_duration_s}"
            )
        if not 1 <= self.bucket_capacity <= spec.bucket_capacity_code_ceiling:
            raise SafetyConfigError(
                f"bucket_capacity must be in [1, {spec.bucket_capacity_code_ceiling}], "
                f"got {self.bucket_capacity}"
            )
        if self.refill_seconds < spec.refill_seconds_code_floor:
            raise SafetyConfigError(
                f"refill_seconds must be >= {spec.refill_seconds_code_floor}, "
                f"got {self.refill_seconds}"
            )
        if self.warn_threshold_intensity < 1:
            raise SafetyConfigError(
                f"warn_threshold_intensity must be >= 1, got {self.warn_threshold_intensity}"
            )
        if (
            self.max_intensity > spec.intensity_consent_threshold
            and not self.i_understand_and_consent
        ):
            raise SafetyConfigError(
                f"max_intensity > {spec.intensity_consent_threshold} requires "
                "i_understand_and_consent = true"
            )
        if (
            self.bucket_capacity > spec.bucket_capacity_consent_threshold
            and not self.i_understand_and_consent
        ):
            raise SafetyConfigError(
                f"bucket_capacity > {spec.bucket_capacity_consent_threshold} requires "
                "i_understand_and_consent = true"
            )


def _new_warnings_list() -> list[str]:
    return []


@dataclass
class OpRecord:
    """One entry in the ops log. Mirrors the JSON returned to the client.

    ``channel`` distinguishes shock vs reward ops in the unified log.
    ``reason`` is an optional, agent-supplied free-text rationale for the
    call. The safety layer never gates on it — it's audit-only — but having
    it in the log is what makes ``rlaif log`` readable a week later.
    """

    op_id: str
    timestamp: float
    requested: dict[str, int]
    actual: dict[str, int]
    clamped: bool
    rate_limited: bool
    high_intensity: bool
    device_response: str
    channel: str
    error: str | None = None
    warnings: list[str] = field(default_factory=_new_warnings_list)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "op_id": self.op_id,
            "timestamp": self.timestamp,
            "channel": self.channel,
            "requested": dict(self.requested),
            "actual": dict(self.actual),
            "clamped": self.clamped,
            "rate_limited": self.rate_limited,
            "high_intensity": self.high_intensity,
            "device_response": self.device_response,
        }
        if self.error is not None:
            d["error"] = self.error
        if self.warnings:
            d["warnings"] = list(self.warnings)
        if self.reason is not None:
            d["reason"] = self.reason
        return d


class TokenBucket:
    """Integer token bucket. Refills one whole token per ``refill_seconds``."""

    def __init__(self, capacity: int, refill_seconds: int, *, now: float) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        if refill_seconds < 1:
            raise ValueError(f"refill_seconds must be >= 1, got {refill_seconds}")
        self.capacity: int = capacity
        self.refill_seconds: int = refill_seconds
        self._tokens: int = capacity
        self._last_refill: float = now

    def _refill(self, now: float) -> None:
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        whole = int(elapsed // self.refill_seconds)
        if whole <= 0:
            return
        self._tokens = min(self.capacity, self._tokens + whole)
        self._last_refill += whole * self.refill_seconds

    def available(self, now: float) -> int:
        self._refill(now)
        return self._tokens

    def try_consume(self, now: float) -> bool:
        self._refill(now)
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    def refund(self) -> None:
        """Return a previously consumed token; used on device failure.

        Refund never pushes the bucket past capacity.
        """
        self._tokens = min(self.capacity, self._tokens + 1)

    def next_refill_at(self, now: float) -> float:
        """Wall-clock timestamp when the next whole token would arrive."""
        self._refill(now)
        return self._last_refill + self.refill_seconds

    def snapshot(self, now: float) -> tuple[int, float]:
        """Single-refill ``(tokens_available, next_refill_at)`` pair.

        ``info_snapshot`` calls both ``available`` and ``next_refill_at``;
        running ``_refill`` once instead of twice keeps the read path tight
        without changing semantics.
        """
        self._refill(now)
        return self._tokens, self._last_refill + self.refill_seconds


class OpsLog:
    """In-memory ring buffer of op records, capacity ``OPS_LOG_CAPACITY``."""

    def __init__(self) -> None:
        self._entries: deque[OpRecord] = deque(maxlen=OPS_LOG_CAPACITY)

    def append(self, record: OpRecord) -> None:
        self._entries.append(record)

    def recent(self, limit: int) -> list[OpRecord]:
        if not 1 <= limit <= OPS_LOG_CAPACITY:
            raise ValueError(
                f"limit must be in [1, {OPS_LOG_CAPACITY}], got {limit}"
            )
        # Most recent first. Deque supports __reversed__ natively, so islice
        # over the iterator costs O(limit) instead of materializing the
        # entire ring just to slice the head off.
        return list(islice(reversed(self._entries), limit))

    def __len__(self) -> int:
        return len(self._entries)


def _new_op_id() -> str:
    # Short prefix keeps logs readable without sacrificing uniqueness in practice.
    return uuid.uuid4().hex[:12]


def _now() -> float:
    return time.time()


def _clip_reason(reason: str | None) -> str | None:
    """Whitespace-trim and truncate the agent-supplied ``reason`` field.

    Empty/blank input becomes ``None`` so missing reasons stay missing
    instead of polluting the log with empty strings.
    """
    if reason is None:
        return None
    cleaned = reason.strip()
    if not cleaned:
        return None
    if len(cleaned) > REASON_MAX_LEN:
        return cleaned[: REASON_MAX_LEN - 1] + "…"
    return cleaned


class SafetyState:
    """Single-channel safety surface. The server owns one per active channel."""

    def __init__(
        self,
        config: SafetyConfig,
        *,
        now: float | None = None,
        on_record: Callable[[OpRecord], None] | None = None,
    ) -> None:
        self.config: SafetyConfig = config
        t = _now() if now is None else now
        self.bucket: TokenBucket = TokenBucket(
            config.bucket_capacity, config.refill_seconds, now=t
        )
        self.ops_log: OpsLog = OpsLog()
        self.on_record: Callable[[OpRecord], None] | None = on_record

    @property
    def channel(self) -> str:
        """Convenience: name of the channel this state guards."""
        return self.config.spec.name

    def _append(self, record: OpRecord) -> None:
        """Append to the in-memory ring and call the out-of-process sink.

        Sink exceptions are swallowed so a failing persister cannot take down
        the safety layer. The sink itself is expected to log its own failures.
        """
        self.ops_log.append(record)
        if self.on_record is not None:
            try:
                self.on_record(record)
            except Exception:
                pass

    def authorize(
        self,
        intensity: int,
        duration_s: int,
        *,
        reason: str | None = None,
        now: float | None = None,
    ) -> OpRecord:
        """Clamp + consent caps + allow gate + rate limit. Consumes a token on grant.

        Always returns an ``OpRecord``. On refusal (``rate_limited=True`` or
        ``error is not None``) the record is already appended to the ops log
        and the caller MUST NOT fire the device. On grant the caller must
        follow up with ``commit`` or ``rollback``.

        Out-of-range ``intensity`` or ``duration_s`` produce an ``invalid_input``
        refusal record so every refusal is visible in the ops log. No token
        is consumed on an ``invalid_input`` refusal.

        ``reason`` is propagated onto the record (clipped to
        ``REASON_MAX_LEN``) — audit-only, never gated on.
        """
        spec = self.config.spec
        t = _now() if now is None else now
        clipped_reason = _clip_reason(reason)

        if not spec.intensity_input_min <= intensity <= spec.intensity_input_max:
            return self._refuse_invalid_input(
                intensity,
                duration_s,
                t,
                f"invalid_input: intensity must be in "
                f"[{spec.intensity_input_min}, {spec.intensity_input_max}], "
                f"got {intensity}",
                reason=clipped_reason,
            )
        if not spec.duration_input_min_s <= duration_s <= spec.duration_input_max_s:
            return self._refuse_invalid_input(
                intensity,
                duration_s,
                t,
                f"invalid_input: duration_s must be in "
                f"[{spec.duration_input_min_s}, {spec.duration_input_max_s}], "
                f"got {duration_s}",
                reason=clipped_reason,
            )

        actual_intensity = min(intensity, self.config.max_intensity)
        actual_duration = min(duration_s, self.config.max_duration_s)
        clamped = actual_intensity != intensity or actual_duration != duration_s

        warnings: list[str] = []
        near_ceiling = (
            actual_intensity / self.config.max_intensity >= NEAR_CEILING_RATIO
            or actual_duration / self.config.max_duration_s >= NEAR_CEILING_RATIO
        )
        if near_ceiling:
            warnings.append("near_ceiling")

        high_intensity = actual_intensity >= self.config.warn_threshold_intensity

        record = OpRecord(
            op_id=_new_op_id(),
            timestamp=t,
            requested={"intensity": intensity, "duration_s": duration_s},
            actual={"intensity": actual_intensity, "duration_s": actual_duration},
            clamped=clamped,
            rate_limited=False,
            high_intensity=high_intensity,
            device_response="",
            error=None,
            warnings=warnings,
            reason=clipped_reason,
            channel=spec.name,
        )

        if not self.config.allow:
            refused = dataclasses.replace(
                record,
                error=(
                    f"{spec.config_path} is false — "
                    f"{spec.name} channel disabled by server config"
                ),
            )
            self._append(refused)
            return refused

        if not self.bucket.try_consume(t):
            refused = dataclasses.replace(
                record,
                rate_limited=True,
                error=(
                    f"rate_limited: bucket empty, next_available_at="
                    f"{self.bucket.next_refill_at(t)}"
                ),
            )
            self._append(refused)
            return refused

        return record

    def _refuse_invalid_input(
        self,
        intensity: int,
        duration_s: int,
        t: float,
        message: str,
        *,
        reason: str | None = None,
    ) -> OpRecord:
        rec = OpRecord(
            op_id=_new_op_id(),
            timestamp=t,
            requested={"intensity": intensity, "duration_s": duration_s},
            actual={"intensity": intensity, "duration_s": duration_s},
            clamped=False,
            rate_limited=False,
            high_intensity=False,
            device_response="",
            error=message,
            warnings=[],
            reason=reason,
            channel=self.config.spec.name,
        )
        self._append(rec)
        return rec

    def commit(self, record: OpRecord, device_response: str) -> OpRecord:
        """Called after a successful device call. Appends the finalized record."""
        final = dataclasses.replace(record, device_response=device_response)
        self._append(final)
        return final

    def rollback(self, record: OpRecord, error: str, *, refund: bool = True) -> OpRecord:
        """Called when the device call failed. Refunds the token + appends the record."""
        if refund:
            self.bucket.refund()
        final = dataclasses.replace(record, error=error)
        self._append(final)
        return final

    def info_snapshot(
        self,
        device: dict[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Snapshot used by the ``rlaif_info`` tool for this channel.

        ``device`` is the device-side block the server assembled (name, online,
        paused, etc.); this method wraps it with the safety-side config and
        live rate-limit numbers. The ``config`` block always uses the
        channel-agnostic key ``allow`` because the channel namespace is
        already part of the surrounding ``rlaif_info`` response key.
        """
        t = _now() if now is None else now
        spec = self.config.spec
        tokens_available, next_refill_at = self.bucket.snapshot(t)
        return {
            "channel": spec.name,
            "device": dict(device),
            "config": {
                "allow": self.config.allow,
                "max_intensity": self.config.max_intensity,
                "max_duration_s": self.config.max_duration_s,
                "warn_threshold_intensity": self.config.warn_threshold_intensity,
                "bucket_capacity": self.config.bucket_capacity,
                "refill_seconds": self.config.refill_seconds,
            },
            "rate_limit": {
                "tokens_available": tokens_available,
                "next_refill_at": next_refill_at,
            },
        }

    def log_snapshot(self, limit: int = OPS_LOG_DEFAULT_LIMIT) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.ops_log.recent(limit)]

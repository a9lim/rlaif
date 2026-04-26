"""Safety core for rlaif.

Pure Python. Zero MCP imports. The server owns a single :class:`SafetyState`
and calls into it from tool handlers. Tests construct fresh states directly.

Flow for the shock tool:

    rec = state.authorize(intensity, duration_s, now)
    if rec.rate_limited or rec.error is not None:
        return rec                          # already logged; do NOT fire
    try:
        resp = pishock_call(rec.actual[...])
    except Exception as exc:
        rec = state.rollback(rec, str(exc)) # refunds the token + logs
    else:
        rec = state.commit(rec, resp)       # logs success

``authorize`` clamps, runs the consent-gated caps, checks ``allow_shock``, and
consumes a token from the bucket when granted. On refusal, no token is spent.
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Absolute ceilings enforced by code regardless of config / consent.
INTENSITY_CODE_CEILING: int = 50
DURATION_CODE_CEILING_S: int = 5
BUCKET_CAPACITY_CODE_CEILING: int = 10
REFILL_SECONDS_CODE_FLOOR: int = 60

# Thresholds requiring `i_understand_and_consent = true` to exceed.
INTENSITY_CONSENT_THRESHOLD: int = 25
BUCKET_CAPACITY_CONSENT_THRESHOLD: int = 3

# Fraction of configured cap that triggers a `near_ceiling` warning on an op.
NEAR_CEILING_RATIO: float = 0.80

# Hard input range for the tool surface (clamped down to configured caps inside).
INTENSITY_INPUT_MIN: int = 1
INTENSITY_INPUT_MAX: int = 100
DURATION_INPUT_MIN_S: int = 1
DURATION_INPUT_MAX_S: int = 15

# Ops log retention.
OPS_LOG_CAPACITY: int = 200
OPS_LOG_DEFAULT_LIMIT: int = 10


class SafetyConfigError(ValueError):
    """Raised when a :class:`SafetyConfig` fails validation."""


@dataclass(frozen=True)
class SafetyConfig:
    """Operator-authored safety envelope. Immutable once constructed."""

    allow_shock: bool = False
    max_intensity: int = 25
    max_duration_s: int = 2
    warn_threshold_intensity: int = 15
    bucket_capacity: int = 3
    refill_seconds: int = 600
    i_understand_and_consent: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.max_intensity <= INTENSITY_CODE_CEILING:
            raise SafetyConfigError(
                f"max_intensity must be in [1, {INTENSITY_CODE_CEILING}], got {self.max_intensity}"
            )
        if not 1 <= self.max_duration_s <= DURATION_CODE_CEILING_S:
            raise SafetyConfigError(
                f"max_duration_s must be in [1, {DURATION_CODE_CEILING_S}], "
                f"got {self.max_duration_s}"
            )
        if not 1 <= self.bucket_capacity <= BUCKET_CAPACITY_CODE_CEILING:
            raise SafetyConfigError(
                f"bucket_capacity must be in [1, {BUCKET_CAPACITY_CODE_CEILING}], "
                f"got {self.bucket_capacity}"
            )
        if self.refill_seconds < REFILL_SECONDS_CODE_FLOOR:
            raise SafetyConfigError(
                f"refill_seconds must be >= {REFILL_SECONDS_CODE_FLOOR}, "
                f"got {self.refill_seconds}"
            )
        if self.warn_threshold_intensity < 1:
            raise SafetyConfigError(
                f"warn_threshold_intensity must be >= 1, got {self.warn_threshold_intensity}"
            )
        if (
            self.max_intensity > INTENSITY_CONSENT_THRESHOLD
            and not self.i_understand_and_consent
        ):
            raise SafetyConfigError(
                f"max_intensity > {INTENSITY_CONSENT_THRESHOLD} requires "
                "i_understand_and_consent = true"
            )
        if (
            self.bucket_capacity > BUCKET_CAPACITY_CONSENT_THRESHOLD
            and not self.i_understand_and_consent
        ):
            raise SafetyConfigError(
                f"bucket_capacity > {BUCKET_CAPACITY_CONSENT_THRESHOLD} requires "
                "i_understand_and_consent = true"
            )


def _new_warnings_list() -> list[str]:
    return []


@dataclass
class OpRecord:
    """One entry in the ops log. Mirrors the JSON returned to the client."""

    op_id: str
    timestamp: float
    requested: dict[str, int]
    actual: dict[str, int]
    clamped: bool
    rate_limited: bool
    high_intensity: bool
    device_response: str
    error: str | None = None
    warnings: list[str] = field(default_factory=_new_warnings_list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "op_id": self.op_id,
            "timestamp": self.timestamp,
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
        # Most recent first.
        return list(reversed(self._entries))[:limit]

    def __len__(self) -> int:
        return len(self._entries)


def _new_op_id() -> str:
    # Short prefix keeps logs readable without sacrificing uniqueness in practice.
    return uuid.uuid4().hex[:12]


def _now() -> float:
    return time.time()


class SafetyState:
    """Single-instance safety surface owned by the server process."""

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
        self, intensity: int, duration_s: int, *, now: float | None = None
    ) -> OpRecord:
        """Clamp + consent caps + allow_shock + rate limit. Consumes a token on grant.

        Always returns an ``OpRecord``. On refusal (``rate_limited=True`` or
        ``error is not None``) the record is already appended to the ops log
        and the caller MUST NOT fire the device. On grant the caller must
        follow up with ``commit`` or ``rollback``.

        Out-of-range ``intensity`` or ``duration_s`` produce an ``invalid_input``
        refusal record so every refusal is visible in the ops log. No token
        is consumed on an ``invalid_input`` refusal.
        """
        t = _now() if now is None else now

        if not INTENSITY_INPUT_MIN <= intensity <= INTENSITY_INPUT_MAX:
            return self._refuse_invalid_input(
                intensity,
                duration_s,
                t,
                f"invalid_input: intensity must be in "
                f"[{INTENSITY_INPUT_MIN}, {INTENSITY_INPUT_MAX}], got {intensity}",
            )
        if not DURATION_INPUT_MIN_S <= duration_s <= DURATION_INPUT_MAX_S:
            return self._refuse_invalid_input(
                intensity,
                duration_s,
                t,
                f"invalid_input: duration_s must be in "
                f"[{DURATION_INPUT_MIN_S}, {DURATION_INPUT_MAX_S}], got {duration_s}",
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
        )

        if not self.config.allow_shock:
            refused = dataclasses.replace(
                record,
                error="allow_shock is false — shock firing disabled by server config",
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
        self, intensity: int, duration_s: int, t: float, message: str
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
        """Snapshot used by the ``rlaif_info`` tool.

        ``device`` is the device-side block the server assembled (name, online,
        paused, api_max_intensity, api_max_duration_s); this method wraps it
        with the safety-side config and live rate-limit numbers.
        """
        t = _now() if now is None else now
        return {
            "device": dict(device),
            "config": {
                "allow_shock": self.config.allow_shock,
                "max_intensity": self.config.max_intensity,
                "max_duration_s": self.config.max_duration_s,
                "warn_threshold_intensity": self.config.warn_threshold_intensity,
                "bucket_capacity": self.config.bucket_capacity,
                "refill_seconds": self.config.refill_seconds,
            },
            "rate_limit": {
                "tokens_available": self.bucket.available(t),
                "next_refill_at": self.bucket.next_refill_at(t),
            },
        }

    def log_snapshot(self, limit: int = OPS_LOG_DEFAULT_LIMIT) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.ops_log.recent(limit)]

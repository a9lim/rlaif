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

``authorize`` clamps, runs the consent-gated caps, checks ``allow``, checks
the shared :class:`FireGate`, and consumes a token from the bucket when
granted. On refusal, no token is spent.

One stimulus at a time, server-wide: both channels' states share one
:class:`FireGate`. A grant reserves it; ``commit`` / ``rollback`` release it
into a holdoff covering the burst the device may still be running. Calls
that arrive meanwhile are refused as ``busy`` rather than queued.

Thread safety: MCP SDK 2.x runs sync tool handlers on worker threads,
concurrently, so every read and write of safety state happens under the
gate's lock. Bucket and gate math run on a monotonic clock; record
timestamps and the ``next_*_at`` values handed to clients are wall-clock.
"""

from __future__ import annotations

import dataclasses
import threading
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

# How long the fire gate stays shut beyond a burst's own duration. Shock
# backends answer when they accept a command, not when the device starts;
# this absorbs that delivery latency. It narrows the overlap window — it
# cannot prove non-overlap against a backend that sits on commands longer.
BURST_HOLDOFF_MARGIN_S: float = 1.0


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
    """Per-channel safety constants, defaults, and labels.

    ``config_path`` is the dotted operator-readable path of the allow flag
    in TOML (``negative.safety.allow`` / ``positive.safety.allow``). It
    lands in refusal messages so the operator can fix the offending key
    in their config without grepping for what's where.

    The ``default_*`` fields are the values a brand-new ``[<channel>.safety]``
    block (or one with that field omitted) ends up with — they match the
    README's defaults table byte-for-byte. Negative defaults are conservative
    on purpose (consent gate kicks in just past them); positive defaults are
    looser because the device cannot hurt the operator.
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
    default_max_intensity: int
    default_max_duration_s: int
    default_warn_threshold_intensity: int
    default_bucket_capacity: int
    default_refill_seconds: int
    # True when a successful device call returns only after the burst has
    # been explicitly stopped (the RewardProvider contract), so the fire
    # gate can reopen right away. False means success only proves the
    # backend accepted the command; the gate then holds for the burst.
    stop_confirmed_on_success: bool = False


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
    default_max_intensity=25,
    default_max_duration_s=2,
    default_warn_threshold_intensity=15,
    default_bucket_capacity=3,
    default_refill_seconds=600,
)

# The positive channel's ceilings are deliberately permissive. Vibration is
# not medically risky and the threat model is "agent spams reward signals
# during a runaway loop" rather than "agent harms the operator". The
# disconnect watchdog (provider-side) is what stops a stuck device, not
# this layer's caps. Consent thresholds equal the code ceilings, which
# means consent is effectively never required on the positive channel.
#
# ``default_warn_threshold_intensity`` mirrors ``default_max_intensity`` so
# every fired op lands above-threshold by default; the field exists for
# parity with the negative channel rather than as a behavioral knob here.
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
    default_max_intensity=75,
    default_max_duration_s=5,
    default_warn_threshold_intensity=75,
    default_bucket_capacity=5,
    default_refill_seconds=30,
    stop_confirmed_on_success=True,
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

    The dataclass-level field defaults match the negative channel so
    bare ``SafetyConfig()`` keeps producing a sane negative-channel
    envelope (used heavily in tests). Code that builds a config from a
    user-facing source (the TOML loader, ``rlaif init``) should use
    :meth:`for_spec` so the missing fields fall back to the *channel's*
    defaults — that's what makes positive defaults different from
    negative ones at load time.
    """

    spec: ChannelSpec = NEGATIVE_CHANNEL
    allow: bool = False
    max_intensity: int = 25
    max_duration_s: int = 2
    warn_threshold_intensity: int = 15
    bucket_capacity: int = 3
    refill_seconds: int = 600
    i_understand_and_consent: bool = False

    @classmethod
    def for_spec(cls, spec: ChannelSpec, **overrides: Any) -> SafetyConfig:
        """Build a :class:`SafetyConfig` whose unset fields fall back to
        ``spec``'s per-channel defaults rather than the dataclass-level
        negative defaults.

        ``overrides`` accepts the same keyword arguments as the constructor
        (``allow``, ``max_intensity``, …). Any field not in ``overrides``
        is filled from the matching ``spec.default_*`` value.
        """
        defaults: dict[str, Any] = {
            "max_intensity": spec.default_max_intensity,
            "max_duration_s": spec.default_max_duration_s,
            "warn_threshold_intensity": spec.default_warn_threshold_intensity,
            "bucket_capacity": spec.default_bucket_capacity,
            "refill_seconds": spec.default_refill_seconds,
        }
        defaults.update(overrides)
        return cls(spec=spec, **defaults)

    def __post_init__(self) -> None:
        spec = self.spec
        if not 1 <= self.max_intensity <= spec.intensity_code_ceiling:
            raise SafetyConfigError(
                f"max_intensity must be in [1, {spec.intensity_code_ceiling}], got {self.max_intensity}"
            )
        if not 1 <= self.max_duration_s <= spec.duration_code_ceiling_s:
            raise SafetyConfigError(
                f"max_duration_s must be in [1, {spec.duration_code_ceiling_s}], got {self.max_duration_s}"
            )
        if not 1 <= self.bucket_capacity <= spec.bucket_capacity_code_ceiling:
            raise SafetyConfigError(
                f"bucket_capacity must be in [1, {spec.bucket_capacity_code_ceiling}], got {self.bucket_capacity}"
            )
        if self.refill_seconds < spec.refill_seconds_code_floor:
            raise SafetyConfigError(
                f"refill_seconds must be >= {spec.refill_seconds_code_floor}, got {self.refill_seconds}"
            )
        if self.warn_threshold_intensity < 1:
            raise SafetyConfigError(f"warn_threshold_intensity must be >= 1, got {self.warn_threshold_intensity}")
        if self.max_intensity > spec.intensity_consent_threshold and not self.i_understand_and_consent:
            raise SafetyConfigError(
                f"max_intensity > {spec.intensity_consent_threshold} requires i_understand_and_consent = true"
            )
        if self.bucket_capacity > spec.bucket_capacity_consent_threshold and not self.i_understand_and_consent:
            raise SafetyConfigError(
                f"bucket_capacity > {spec.bucket_capacity_consent_threshold} requires i_understand_and_consent = true"
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
        """When the next whole token would arrive, on ``now``'s clock."""
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
            raise ValueError(f"limit must be in [1, {OPS_LOG_CAPACITY}], got {limit}")
        # Most recent first. Deque supports __reversed__ natively, so islice
        # over the iterator costs O(limit) instead of materializing the
        # entire ring just to slice the head off.
        return list(islice(reversed(self._entries), limit))

    def __len__(self) -> int:
        return len(self._entries)


class FireGate:
    """Server-wide one-stimulus-at-a-time reservation, shared by every channel.

    A granted ``authorize`` reserves the gate for its op; ``commit`` /
    ``rollback`` release it into a holdoff measured from when the device
    call returned, because a backend can return before the burst ends (or
    fail after the command already went out). While reserved or holding
    off, the gate refuses everyone — nothing queues, so nothing can fire
    after the client has stopped waiting for it.

    ``lock`` doubles as the lock for every :class:`SafetyState` sharing the
    gate. Callers hold it around every method here.

    Times are monotonic; ``-inf`` means "never held off".
    """

    def __init__(self) -> None:
        self.lock: threading.RLock = threading.RLock()
        self._holder: tuple[str, str] | None = None  # (channel, op_id)
        self._last_channel: str | None = None
        self._expected_until: float = float("-inf")
        self._holdoff_until: float = float("-inf")

    def busy_until(self, now: float) -> float | None:
        """Monotonic time the gate is expected to reopen, or ``None`` if open.

        While a burst is in flight this is an estimate (reserve time + its
        duration + margin); the real holdoff is fixed when it returns.
        """
        if self._holder is not None:
            return max(self._expected_until, self._holdoff_until)
        if now < self._holdoff_until:
            return self._holdoff_until
        return None

    def busy_channel(self) -> str | None:
        """Channel holding the gate, or the one whose holdoff is running."""
        return self._holder[0] if self._holder is not None else self._last_channel

    def reserve(self, channel: str, op_id: str, *, now: float, expected_s: float) -> None:
        if self._holder is not None:
            raise RuntimeError(f"fire gate already held by {self._holder[0]} op {self._holder[1]}")
        self._holder = (channel, op_id)
        self._last_channel = channel
        self._expected_until = now + expected_s

    def release(self, op_id: str, *, now: float, holdoff_s: float) -> None:
        if self._holder is None or self._holder[1] != op_id:
            raise RuntimeError(f"op {op_id} does not hold the fire gate")
        self._holder = None
        self._holdoff_until = max(self._holdoff_until, now + holdoff_s)


def _new_op_id() -> str:
    # Short prefix keeps logs readable without sacrificing uniqueness in practice.
    return uuid.uuid4().hex[:12]


def _wall() -> float:
    return time.time()


def _monotonic() -> float:
    return time.monotonic()


def _clocks(now: float | None) -> tuple[float, float]:
    """``(monotonic, wall)`` for this instant.

    An injected ``now`` (tests, dry-run) stands in for both clocks so a
    synthetic timeline stays self-consistent.
    """
    if now is not None:
        return now, now
    return _monotonic(), _wall()


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
    """Single-channel safety surface. The server owns one per active channel.

    ``gate`` is the server-wide :class:`FireGate`; pass the same instance to
    every channel's state. A state built without one gets a private gate,
    which is what single-channel tests want.
    """

    def __init__(
        self,
        config: SafetyConfig,
        *,
        gate: FireGate | None = None,
        now: float | None = None,
        on_record: Callable[[OpRecord], None] | None = None,
    ) -> None:
        self.config: SafetyConfig = config
        self.gate: FireGate = gate if gate is not None else FireGate()
        self._lock: threading.RLock = self.gate.lock
        t, _ = _clocks(now)
        self.bucket: TokenBucket = TokenBucket(config.bucket_capacity, config.refill_seconds, now=t)
        self.ops_log: OpsLog = OpsLog()
        self.on_record: Callable[[OpRecord], None] | None = on_record
        # op_ids granted by ``authorize`` and not yet finalized. Guards
        # against a stale or duplicate commit/rollback releasing the gate
        # for some other op or refunding twice.
        self._pending: set[str] = set()

    @property
    def channel(self) -> str:
        """Convenience: name of the channel this state guards."""
        return self.config.spec.name

    def _append(self, record: OpRecord) -> None:
        """Append to the in-memory ring and call the out-of-process sink.

        Runs under the gate lock, so sink writes from both channels are
        serialized too. Sink exceptions are swallowed so a failing persister
        cannot take down the safety layer. The sink itself is expected to
        log its own failures.
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
        """Clamp + consent caps + allow gate + fire gate + rate limit.

        Consumes a token and reserves the fire gate on grant.

        Always returns an ``OpRecord``. On refusal (``rate_limited=True`` or
        ``error is not None``) the record is already appended to the ops log
        and the caller MUST NOT fire the device. On grant the caller must
        follow up with ``commit`` or ``rollback`` — the gate stays reserved
        until it does.

        Out-of-range ``intensity`` or ``duration_s`` produce an ``invalid_input``
        refusal record so every refusal is visible in the ops log. No token
        is consumed on any refusal.

        ``reason`` is propagated onto the record (clipped to
        ``REASON_MAX_LEN``) — audit-only, never gated on.
        """
        spec = self.config.spec
        clipped_reason = _clip_reason(reason)

        with self._lock:
            t_mono, t_wall = _clocks(now)

            if not spec.intensity_input_min <= intensity <= spec.intensity_input_max:
                return self._refuse_invalid_input(
                    intensity,
                    duration_s,
                    t_wall,
                    f"invalid_input: intensity must be in "
                    f"[{spec.intensity_input_min}, {spec.intensity_input_max}], "
                    f"got {intensity}",
                    reason=clipped_reason,
                )
            if not spec.duration_input_min_s <= duration_s <= spec.duration_input_max_s:
                return self._refuse_invalid_input(
                    intensity,
                    duration_s,
                    t_wall,
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
                timestamp=t_wall,
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
                    error=(f"{spec.config_path} is false — {spec.name} channel disabled by server config"),
                )
                self._append(refused)
                return refused

            busy_until = self.gate.busy_until(t_mono)
            if busy_until is not None:
                refused = dataclasses.replace(
                    record,
                    error=(
                        f"busy: a {self.gate.busy_channel()} stimulus is in progress, "
                        f"next_available_at={t_wall + (busy_until - t_mono)}"
                    ),
                )
                self._append(refused)
                return refused

            if not self.bucket.try_consume(t_mono):
                next_refill = t_wall + (self.bucket.next_refill_at(t_mono) - t_mono)
                refused = dataclasses.replace(
                    record,
                    rate_limited=True,
                    error=f"rate_limited: bucket empty, next_available_at={next_refill}",
                )
                self._append(refused)
                return refused

            self.gate.reserve(
                spec.name,
                record.op_id,
                now=t_mono,
                expected_s=actual_duration + BURST_HOLDOFF_MARGIN_S,
            )
            self._pending.add(record.op_id)
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

    def _finalize(self, record: OpRecord, *, now: float | None, holdoff_s: float) -> None:
        """Retire a granted op and release the gate into its holdoff."""
        if record.op_id not in self._pending:
            raise RuntimeError(f"op {record.op_id} is not pending on the {self.channel} channel")
        self._pending.discard(record.op_id)
        t_mono, _ = _clocks(now)
        self.gate.release(record.op_id, now=t_mono, holdoff_s=holdoff_s)

    def commit(self, record: OpRecord, device_response: str, *, now: float | None = None) -> OpRecord:
        """Called after a successful device call. Appends the finalized record.

        The gate holds for the burst unless this channel's providers only
        return once the burst is explicitly stopped.
        """
        with self._lock:
            holdoff_s = BURST_HOLDOFF_MARGIN_S
            if not self.config.spec.stop_confirmed_on_success:
                holdoff_s += record.actual["duration_s"]
            self._finalize(record, now=now, holdoff_s=holdoff_s)
            final = dataclasses.replace(record, device_response=device_response)
            self._append(final)
            return final

    def rollback(
        self,
        record: OpRecord,
        error: str,
        *,
        refund: bool = True,
        now: float | None = None,
    ) -> OpRecord:
        """Called when the device call failed. Refunds the token + appends the record.

        The gate always holds for the full burst: a failure (a timeout, a
        lost response, a stop that never arrived) does not prove the device
        stayed idle. Refund and gate release are separate decisions.
        """
        with self._lock:
            self._finalize(
                record,
                now=now,
                holdoff_s=record.actual["duration_s"] + BURST_HOLDOFF_MARGIN_S,
            )
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

        ``rate_limit.busy_until`` is the wall-clock time the shared fire gate
        reopens (an estimate while a burst is in flight), or ``None`` when it
        is open.
        """
        spec = self.config.spec
        with self._lock:
            t_mono, t_wall = _clocks(now)
            tokens_available, next_refill = self.bucket.snapshot(t_mono)
            busy_until = self.gate.busy_until(t_mono)
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
                    "next_refill_at": t_wall + (next_refill - t_mono),
                    "busy_until": None if busy_until is None else t_wall + (busy_until - t_mono),
                },
            }

    def recent_ops(self, limit: int = OPS_LOG_DEFAULT_LIMIT) -> tuple[list[OpRecord], int]:
        """``(most recent records, total retained)`` read under the lock."""
        with self._lock:
            return self.ops_log.recent(limit), len(self.ops_log)

    def log_snapshot(self, limit: int = OPS_LOG_DEFAULT_LIMIT) -> list[dict[str, Any]]:
        records, _ = self.recent_ops(limit)
        return [r.to_dict() for r in records]

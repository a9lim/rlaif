"""Safety spec for rlaif.

This file reads as the normative description of what the server will and will
not do. If you are auditing the safety layer, read this file first.
"""

from __future__ import annotations

import threading

import pytest

from rlaif.safety import (
    BUCKET_CAPACITY_CODE_CEILING,
    BUCKET_CAPACITY_CONSENT_THRESHOLD,
    BURST_HOLDOFF_MARGIN_S,
    DURATION_CODE_CEILING_S,
    INTENSITY_CODE_CEILING,
    INTENSITY_CONSENT_THRESHOLD,
    NEGATIVE_CHANNEL,
    OPS_LOG_CAPACITY,
    POSITIVE_CHANNEL,
    REFILL_SECONDS_CODE_FLOOR,
    ChannelSpec,
    FireGate,
    OpRecord,
    OpsLog,
    SafetyConfig,
    SafetyConfigError,
    SafetyState,
    TokenBucket,
    _new_op_id,
)

# ---------------------------------------------------------------------------
# SafetyConfig: ceilings and the safety gate
# ---------------------------------------------------------------------------


class TestSafetyConfigCeilings:
    def test_defaults_are_conservative(self) -> None:
        c = SafetyConfig()
        assert c.spec is NEGATIVE_CHANNEL
        assert c.allow is False
        assert c.max_intensity == 25
        assert c.max_duration_s == 2
        assert c.warn_threshold_intensity == 15
        assert c.bucket_capacity == 3
        assert c.refill_seconds == 600
        assert c.i_understand_and_consent is False

    def test_max_intensity_code_ceiling(self) -> None:
        with pytest.raises(SafetyConfigError, match="max_intensity"):
            SafetyConfig(
                max_intensity=INTENSITY_CODE_CEILING + 1,
                i_understand_and_consent=True,
            )

    def test_max_duration_code_ceiling(self) -> None:
        with pytest.raises(SafetyConfigError, match="max_duration_s"):
            SafetyConfig(max_duration_s=DURATION_CODE_CEILING_S + 1)

    def test_bucket_capacity_code_ceiling(self) -> None:
        with pytest.raises(SafetyConfigError, match="bucket_capacity"):
            SafetyConfig(
                bucket_capacity=BUCKET_CAPACITY_CODE_CEILING + 1,
                i_understand_and_consent=True,
            )

    def test_refill_seconds_code_floor(self) -> None:
        with pytest.raises(SafetyConfigError, match="refill_seconds"):
            SafetyConfig(refill_seconds=REFILL_SECONDS_CODE_FLOOR - 1)

    def test_min_values_rejected(self) -> None:
        with pytest.raises(SafetyConfigError):
            SafetyConfig(max_intensity=0)
        with pytest.raises(SafetyConfigError):
            SafetyConfig(max_duration_s=0)
        with pytest.raises(SafetyConfigError):
            SafetyConfig(bucket_capacity=0)


class TestSafetyGate:
    def test_max_intensity_above_threshold_without_consent_rejected(self) -> None:
        with pytest.raises(SafetyConfigError, match="i_understand_and_consent"):
            SafetyConfig(max_intensity=INTENSITY_CONSENT_THRESHOLD + 1)

    def test_max_intensity_above_threshold_with_consent_allowed(self) -> None:
        c = SafetyConfig(
            max_intensity=INTENSITY_CONSENT_THRESHOLD + 1,
            i_understand_and_consent=True,
        )
        assert c.max_intensity == INTENSITY_CONSENT_THRESHOLD + 1

    def test_max_intensity_at_threshold_no_consent_needed(self) -> None:
        c = SafetyConfig(max_intensity=INTENSITY_CONSENT_THRESHOLD)
        assert c.max_intensity == INTENSITY_CONSENT_THRESHOLD

    def test_bucket_capacity_above_threshold_without_consent_rejected(self) -> None:
        with pytest.raises(SafetyConfigError, match="i_understand_and_consent"):
            SafetyConfig(bucket_capacity=BUCKET_CAPACITY_CONSENT_THRESHOLD + 1)

    def test_bucket_capacity_above_threshold_with_consent_allowed(self) -> None:
        c = SafetyConfig(
            bucket_capacity=BUCKET_CAPACITY_CONSENT_THRESHOLD + 1,
            i_understand_and_consent=True,
        )
        assert c.bucket_capacity == BUCKET_CAPACITY_CONSENT_THRESHOLD + 1

    def test_at_consent_and_code_ceiling(self) -> None:
        c = SafetyConfig(
            max_intensity=INTENSITY_CODE_CEILING,
            bucket_capacity=BUCKET_CAPACITY_CODE_CEILING,
            i_understand_and_consent=True,
        )
        assert c.max_intensity == INTENSITY_CODE_CEILING
        assert c.bucket_capacity == BUCKET_CAPACITY_CODE_CEILING


# ---------------------------------------------------------------------------
# TokenBucket: drain, refill, cap
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_starts_full(self) -> None:
        b = TokenBucket(capacity=3, refill_seconds=600, now=1000.0)
        assert b.available(1000.0) == 3

    def test_consume_drains_by_one(self) -> None:
        b = TokenBucket(capacity=3, refill_seconds=600, now=0.0)
        assert b.try_consume(0.0) is True
        assert b.available(0.0) == 2
        assert b.try_consume(0.0) is True
        assert b.available(0.0) == 1
        assert b.try_consume(0.0) is True
        assert b.available(0.0) == 0

    def test_empty_bucket_refuses(self) -> None:
        b = TokenBucket(capacity=2, refill_seconds=600, now=0.0)
        assert b.try_consume(0.0)
        assert b.try_consume(0.0)
        assert b.try_consume(0.0) is False
        assert b.available(0.0) == 0

    def test_next_refill_at_when_empty(self) -> None:
        b = TokenBucket(capacity=1, refill_seconds=600, now=1000.0)
        assert b.try_consume(1000.0)
        # Next token arrives at last_refill + refill_seconds.
        assert b.next_refill_at(1000.0) == pytest.approx(1600.0)

    def test_refill_one_token_per_refill_seconds(self) -> None:
        b = TokenBucket(capacity=3, refill_seconds=100, now=0.0)
        # Drain completely.
        for _ in range(3):
            assert b.try_consume(0.0)
        assert b.available(0.0) == 0
        # 99s -> still 0
        assert b.available(99.0) == 0
        # 100s -> exactly 1
        assert b.available(100.0) == 1
        # 250s -> 2 more tokens added (at t=200, t=300 does not fit yet,
        # but 250-100 = 150 covers exactly one more refill interval).
        assert b.available(250.0) == 2
        # 400s -> capped at capacity
        assert b.available(400.0) == 3

    def test_refill_never_exceeds_capacity(self) -> None:
        b = TokenBucket(capacity=3, refill_seconds=60, now=0.0)
        # Massive time jump — bucket must not exceed capacity.
        assert b.available(1_000_000.0) == 3

    def test_refund_never_exceeds_capacity(self) -> None:
        b = TokenBucket(capacity=2, refill_seconds=60, now=0.0)
        b.refund()  # already full — should stay at 2
        assert b.available(0.0) == 2

    def test_refund_restores_one_token(self) -> None:
        b = TokenBucket(capacity=2, refill_seconds=60, now=0.0)
        assert b.try_consume(0.0)
        assert b.available(0.0) == 1
        b.refund()
        assert b.available(0.0) == 2

    def test_fractional_elapsed_does_not_tick(self) -> None:
        b = TokenBucket(capacity=1, refill_seconds=60, now=0.0)
        assert b.try_consume(0.0)
        # 59.999s -> no token yet
        assert b.available(59.999) == 0
        # 60s -> exactly one token
        assert b.available(60.0) == 1

    def test_invalid_capacity(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(capacity=0, refill_seconds=60, now=0.0)

    def test_invalid_refill_seconds(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(capacity=1, refill_seconds=0, now=0.0)


# ---------------------------------------------------------------------------
# OpsLog: ring behavior
# ---------------------------------------------------------------------------


def _stub_record(op_id: str = "x") -> OpRecord:
    return OpRecord(
        op_id=op_id,
        timestamp=0.0,
        requested={"intensity": 1, "duration_s": 1},
        actual={"intensity": 1, "duration_s": 1},
        clamped=False,
        rate_limited=False,
        high_intensity=False,
        device_response="",
        channel="negative",
    )


class TestOpsLog:
    def test_empty(self) -> None:
        log = OpsLog()
        assert len(log) == 0
        assert log.recent(10) == []

    def test_ring_drops_oldest_at_capacity(self) -> None:
        log = OpsLog()
        for i in range(OPS_LOG_CAPACITY + 5):
            log.append(_stub_record(op_id=str(i)))
        assert len(log) == OPS_LOG_CAPACITY
        ids = [r.op_id for r in log.recent(OPS_LOG_CAPACITY)]
        # Ring kept the last OPS_LOG_CAPACITY records.
        assert ids[0] == str(OPS_LOG_CAPACITY + 4)
        assert ids[-1] == str(5)

    def test_recent_returns_most_recent_first(self) -> None:
        log = OpsLog()
        log.append(_stub_record(op_id="a"))
        log.append(_stub_record(op_id="b"))
        log.append(_stub_record(op_id="c"))
        assert [r.op_id for r in log.recent(10)] == ["c", "b", "a"]

    def test_recent_respects_limit(self) -> None:
        log = OpsLog()
        for i in range(10):
            log.append(_stub_record(op_id=str(i)))
        assert [r.op_id for r in log.recent(3)] == ["9", "8", "7"]

    def test_recent_limit_bounds(self) -> None:
        log = OpsLog()
        with pytest.raises(ValueError):
            log.recent(0)
        with pytest.raises(ValueError):
            log.recent(OPS_LOG_CAPACITY + 1)


# ---------------------------------------------------------------------------
# SafetyState: clamping, allow gate, rate limiting, ops log integration
# ---------------------------------------------------------------------------


def _state(**kw: object) -> SafetyState:
    """Build a SafetyState with allow=True by default for convenience."""
    cfg_kwargs: dict[str, object] = {"allow": True}
    cfg_kwargs.update(kw)
    return SafetyState(SafetyConfig(**cfg_kwargs), now=0.0)  # type: ignore[arg-type]


class TestFreshState:
    def test_full_bucket_and_empty_log(self) -> None:
        s = _state()
        assert s.bucket.available(0.0) == s.config.bucket_capacity
        assert len(s.ops_log) == 0


class TestIntensityClamping:
    def test_below_cap_unchanged(self) -> None:
        s = _state(max_intensity=25)
        rec = s.authorize(intensity=10, duration_s=1, now=0.0)
        assert rec.actual["intensity"] == 10
        assert rec.clamped is False
        assert rec.error is None

    def test_at_cap_unchanged_not_clamped(self) -> None:
        s = _state(max_intensity=25)
        rec = s.authorize(intensity=25, duration_s=1, now=0.0)
        assert rec.actual["intensity"] == 25
        assert rec.clamped is False

    def test_above_cap_clamped(self) -> None:
        s = _state(max_intensity=25)
        rec = s.authorize(intensity=80, duration_s=1, now=0.0)
        assert rec.requested["intensity"] == 80
        assert rec.actual["intensity"] == 25
        assert rec.clamped is True
        assert rec.error is None  # still allowed, just clamped

    def test_at_code_ceiling_with_consent(self) -> None:
        s = _state(
            max_intensity=INTENSITY_CODE_CEILING,
            i_understand_and_consent=True,
        )
        rec = s.authorize(intensity=INTENSITY_CODE_CEILING, duration_s=1, now=0.0)
        assert rec.actual["intensity"] == INTENSITY_CODE_CEILING
        assert rec.clamped is False

    def test_at_code_ceiling_without_consent_impossible(self) -> None:
        # Config construction fails outright — no state to authorize from.
        with pytest.raises(SafetyConfigError):
            SafetyConfig(
                max_intensity=INTENSITY_CODE_CEILING,
                i_understand_and_consent=False,
            )

    def test_intensity_input_range_refused_and_logged(self) -> None:
        s = _state()
        before = s.bucket.available(0.0)
        rec = s.authorize(intensity=0, duration_s=1, now=0.0)
        assert rec.error is not None
        assert "invalid_input" in rec.error
        assert "intensity" in rec.error
        # Refusal logged.
        assert len(s.ops_log) == 1
        # No token consumed.
        assert s.bucket.available(0.0) == before

        rec2 = s.authorize(intensity=101, duration_s=1, now=0.0)
        assert rec2.error is not None
        assert "invalid_input" in rec2.error
        assert len(s.ops_log) == 2
        assert s.bucket.available(0.0) == before


class TestDurationClamping:
    def test_below_cap_unchanged(self) -> None:
        s = _state(max_duration_s=2)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert rec.actual["duration_s"] == 1
        assert rec.clamped is False

    def test_at_cap_unchanged(self) -> None:
        s = _state(max_duration_s=2)
        rec = s.authorize(intensity=1, duration_s=2, now=0.0)
        assert rec.actual["duration_s"] == 2
        assert rec.clamped is False

    def test_above_cap_clamped(self) -> None:
        s = _state(max_duration_s=2)
        rec = s.authorize(intensity=1, duration_s=10, now=0.0)
        assert rec.requested["duration_s"] == 10
        assert rec.actual["duration_s"] == 2
        assert rec.clamped is True

    def test_at_code_ceiling(self) -> None:
        s = _state(max_duration_s=DURATION_CODE_CEILING_S)
        rec = s.authorize(intensity=1, duration_s=DURATION_CODE_CEILING_S, now=0.0)
        assert rec.actual["duration_s"] == DURATION_CODE_CEILING_S
        assert rec.clamped is False

    def test_duration_input_range_refused_and_logged(self) -> None:
        s = _state()
        before = s.bucket.available(0.0)
        rec = s.authorize(intensity=1, duration_s=0, now=0.0)
        assert rec.error is not None
        assert "invalid_input" in rec.error
        assert "duration_s" in rec.error
        assert len(s.ops_log) == 1
        assert s.bucket.available(0.0) == before

        rec2 = s.authorize(intensity=1, duration_s=16, now=0.0)
        assert rec2.error is not None
        assert "invalid_input" in rec2.error
        assert len(s.ops_log) == 2
        assert s.bucket.available(0.0) == before


class TestAllowGate:
    def test_false_refuses_even_with_valid_params(self) -> None:
        s = SafetyState(SafetyConfig(allow=False), now=0.0)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert rec.error is not None
        # Error string carries the channel-specific TOML path.
        assert "negative.safety.allow" in rec.error
        assert rec.rate_limited is False
        # Refusal did not consume a token.
        assert s.bucket.available(0.0) == s.config.bucket_capacity
        # Refusal was logged.
        assert len(s.ops_log) == 1

    def test_true_permits_firing(self) -> None:
        s = SafetyState(SafetyConfig(allow=True), now=0.0)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert rec.error is None
        assert rec.rate_limited is False
        # Token was consumed.
        assert s.bucket.available(0.0) == s.config.bucket_capacity - 1


class TestRateLimiting:
    # Fires are spaced 10s apart: past the fire gate's holdoff (duration +
    # margin), well inside the refill interval. Back-to-back fires at one
    # instant are a busy refusal now — see TestFireGate.

    def test_drains_on_success_commit(self) -> None:
        s = _state(bucket_capacity=3)
        for i in range(3):
            t = 10.0 * i
            rec = s.authorize(intensity=1, duration_s=1, now=t)
            assert rec.error is None
            s.commit(rec, "Operation Succeeded.", now=t)
        assert s.bucket.available(20.0) == 0

    def test_refuses_when_empty(self) -> None:
        s = _state(bucket_capacity=2)
        for t in (0.0, 10.0):
            rec = s.authorize(intensity=1, duration_s=1, now=t)
            s.commit(rec, "ok", now=t)
        # Third attempt — bucket empty.
        rec = s.authorize(intensity=1, duration_s=1, now=20.0)
        assert rec.rate_limited is True
        assert rec.error is not None
        assert "next_available_at" in rec.error
        # Logged.
        assert len(s.ops_log) == 3

    def test_next_available_at_after_refusal(self) -> None:
        # Build state at t=0, drain immediately. last_refill stays at 0,
        # so next refill is at 0 + refill_seconds.
        s = _state(bucket_capacity=1, refill_seconds=600)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        s.commit(rec, "ok", now=0.0)
        rec2 = s.authorize(intensity=1, duration_s=1, now=10.0)
        assert rec2.rate_limited is True
        assert s.bucket.next_refill_at(10.0) == pytest.approx(600.0)

    def test_refill_permits_subsequent_fire(self) -> None:
        s = _state(bucket_capacity=1, refill_seconds=100)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        s.commit(rec, "ok", now=0.0)
        # Before refill: refused.
        rec_refused = s.authorize(intensity=1, duration_s=1, now=50.0)
        assert rec_refused.rate_limited is True
        # After refill: granted.
        rec_granted = s.authorize(intensity=1, duration_s=1, now=100.0)
        assert rec_granted.rate_limited is False
        assert rec_granted.error is None


class TestRollbackRefundsToken:
    def test_rollback_restores_bucket(self) -> None:
        s = _state(bucket_capacity=3)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert s.bucket.available(0.0) == 2
        s.rollback(rec, error="device offline")
        assert s.bucket.available(0.0) == 3
        # Both the authorize and the rollback are logged… actually only the
        # finalized record should be in the log (rollback appends).
        assert len(s.ops_log) == 1
        assert s.ops_log.recent(1)[0].error == "device offline"

    def test_rollback_without_refund(self) -> None:
        s = _state(bucket_capacity=3)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        s.rollback(rec, error="punitive", refund=False)
        assert s.bucket.available(0.0) == 2


class TestHighIntensityFlag:
    def test_at_threshold_flagged(self) -> None:
        s = _state(warn_threshold_intensity=15)
        rec = s.authorize(intensity=15, duration_s=1, now=0.0)
        assert rec.high_intensity is True

    def test_below_threshold_not_flagged(self) -> None:
        s = _state(warn_threshold_intensity=15)
        rec = s.authorize(intensity=14, duration_s=1, now=0.0)
        assert rec.high_intensity is False

    def test_above_threshold_flagged(self) -> None:
        s = _state(warn_threshold_intensity=15)
        rec = s.authorize(intensity=20, duration_s=1, now=0.0)
        assert rec.high_intensity is True

    def test_uses_actual_not_requested(self) -> None:
        # Requested 80, cap 10 -> actual 10 < threshold 15 -> not flagged.
        s = _state(max_intensity=10, warn_threshold_intensity=15)
        rec = s.authorize(intensity=80, duration_s=1, now=0.0)
        assert rec.actual["intensity"] == 10
        assert rec.high_intensity is False


class TestNearCeilingWarning:
    def test_at_80pct_of_intensity_cap(self) -> None:
        s = _state(max_intensity=25, max_duration_s=5)
        rec = s.authorize(intensity=20, duration_s=1, now=0.0)
        assert rec.actual["intensity"] == 20
        assert "near_ceiling" in rec.warnings

    def test_just_below_80pct(self) -> None:
        s = _state(max_intensity=25, max_duration_s=5)
        rec = s.authorize(intensity=19, duration_s=1, now=0.0)
        # 19/25 = 0.76 -> not near ceiling
        # 1/5 = 0.20  -> not near ceiling
        assert "near_ceiling" not in rec.warnings

    def test_at_80pct_of_duration_cap(self) -> None:
        s = _state(max_intensity=25, max_duration_s=5)
        rec = s.authorize(intensity=1, duration_s=4, now=0.0)
        assert "near_ceiling" in rec.warnings

    def test_fires_on_clamped_actual(self) -> None:
        # Requested 100 intensity, cap 25 -> actual 25 -> 25/25 = 1.0 -> fires.
        s = _state(max_intensity=25)
        rec = s.authorize(intensity=100, duration_s=1, now=0.0)
        assert rec.actual["intensity"] == 25
        assert "near_ceiling" in rec.warnings


class TestInfoSnapshot:
    def test_shape(self) -> None:
        s = _state(max_intensity=25, bucket_capacity=3, refill_seconds=600)
        snap = s.info_snapshot(
            device={
                "name": "test",
                "online": True,
                "paused": False,
                "api_max_intensity": 100,
                "api_max_duration_s": 15,
            },
            now=0.0,
        )
        assert snap["device"]["name"] == "test"
        assert snap["config"]["max_intensity"] == 25
        assert snap["config"]["allow"] is True
        assert snap["rate_limit"]["tokens_available"] == 3


class TestLogSnapshot:
    def test_serializes_records(self) -> None:
        s = _state()
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        s.commit(rec, "Operation Succeeded.")
        out = s.log_snapshot(limit=10)
        assert len(out) == 1
        entry = out[0]
        assert set(entry) >= {
            "op_id",
            "timestamp",
            "requested",
            "actual",
            "clamped",
            "rate_limited",
            "high_intensity",
            "device_response",
        }
        assert entry["device_response"] == "Operation Succeeded."
        assert "error" not in entry  # error omitted when None

    def test_error_key_only_when_set(self) -> None:
        s = SafetyState(SafetyConfig(allow=False), now=0.0)
        s.authorize(intensity=1, duration_s=1, now=0.0)
        entry = s.log_snapshot(limit=1)[0]
        assert "error" in entry


def test_op_ids_are_unique() -> None:
    ids = {_new_op_id() for _ in range(1000)}
    assert len(ids) == 1000


# ---------------------------------------------------------------------------
# FireGate: one stimulus at a time, server-wide
#
# Both channels' states share one gate. A grant reserves it; commit/rollback
# release it into a holdoff that covers the burst the device may still be
# running. Anything arriving meanwhile is refused as `busy` — logged, no
# token spent, never queued.
# ---------------------------------------------------------------------------


def _shared_pair(**negative: object) -> tuple[SafetyState, SafetyState]:
    gate = FireGate()
    neg_kwargs: dict[str, object] = {"allow": True}
    neg_kwargs.update(negative)
    neg = SafetyState(SafetyConfig(**neg_kwargs), gate=gate, now=0.0)  # type: ignore[arg-type]
    pos = SafetyState(SafetyConfig.for_spec(POSITIVE_CHANNEL, allow=True), gate=gate, now=0.0)
    return neg, pos


class TestFireGate:
    def test_second_call_while_in_flight_is_busy(self) -> None:
        s = _state(bucket_capacity=3)
        first = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert first.error is None
        second = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert second.error is not None
        assert second.error.startswith("busy:")
        assert "next_available_at" in second.error
        assert second.rate_limited is False
        # Only the grant spent a token; the refusal is logged.
        assert s.bucket.available(0.0) == 2
        assert [r.op_id for r in s.ops_log.recent(10)] == [second.op_id]

    def test_shock_holdoff_covers_burst_from_return_time(self) -> None:
        # Shock backends return on acceptance, so the gate holds for the
        # burst, measured from when the device call returned (t=10 here,
        # a slow backend), not from authorize.
        s = _state(max_duration_s=2)
        rec = s.authorize(intensity=1, duration_s=2, now=0.0)
        s.commit(rec, "ok", now=10.0)
        reopen = 10.0 + 2 + BURST_HOLDOFF_MARGIN_S
        assert s.authorize(intensity=1, duration_s=1, now=reopen - 0.01).error is not None
        assert s.authorize(intensity=1, duration_s=1, now=reopen).error is None

    def test_positive_success_reopens_after_margin_only(self) -> None:
        # RewardProviders return only after the explicit stop landed.
        s = SafetyState(SafetyConfig.for_spec(POSITIVE_CHANNEL, allow=True), now=0.0)
        rec = s.authorize(intensity=10, duration_s=5, now=0.0)
        s.commit(rec, "ok", now=5.0)
        reopen = 5.0 + BURST_HOLDOFF_MARGIN_S
        assert s.authorize(intensity=10, duration_s=1, now=reopen - 0.01).error is not None
        assert s.authorize(intensity=10, duration_s=1, now=reopen).error is None

    @pytest.mark.parametrize("refund", [True, False])
    def test_rollback_holds_full_burst_even_on_positive(self, refund: bool) -> None:
        # A failure does not prove the device stayed idle (lost response,
        # stop that never arrived), so rollback always holds the burst.
        s = SafetyState(SafetyConfig.for_spec(POSITIVE_CHANNEL, allow=True), now=0.0)
        rec = s.authorize(intensity=10, duration_s=5, now=0.0)
        s.rollback(rec, error="watchdog: stop did not deliver", refund=refund, now=1.0)
        reopen = 1.0 + 5 + BURST_HOLDOFF_MARGIN_S
        assert s.authorize(intensity=10, duration_s=1, now=reopen - 0.01).error is not None
        assert s.authorize(intensity=10, duration_s=1, now=reopen).error is None

    def test_gate_is_shared_across_channels(self) -> None:
        neg, pos = _shared_pair()
        shock = neg.authorize(intensity=1, duration_s=1, now=0.0)
        assert shock.error is None
        blocked = pos.authorize(intensity=10, duration_s=1, now=0.0)
        assert blocked.error is not None
        assert "negative stimulus is in progress" in blocked.error
        assert blocked.channel == "positive"
        # The positive refusal spent none of the positive bucket.
        assert pos.bucket.available(0.0) == pos.config.bucket_capacity

        neg.commit(shock, "ok", now=0.0)
        reopen = 0.0 + 1 + BURST_HOLDOFF_MARGIN_S
        assert pos.authorize(intensity=10, duration_s=1, now=reopen - 0.01).error is not None
        vibe = pos.authorize(intensity=10, duration_s=1, now=reopen)
        assert vibe.error is None
        assert "positive stimulus" in (neg.authorize(intensity=1, duration_s=1, now=reopen).error or "")

    def test_states_without_a_shared_gate_do_not_block_each_other(self) -> None:
        # Constructor contract: no gate passed = private gate. The server
        # must pass one shared gate to both channels (build_server checks).
        a = _state()
        b = SafetyState(SafetyConfig.for_spec(POSITIVE_CHANNEL, allow=True), now=0.0)
        assert a.authorize(intensity=1, duration_s=1, now=0.0).error is None
        assert b.authorize(intensity=1, duration_s=1, now=0.0).error is None

    def test_busy_refusal_does_not_touch_refill_anchor(self) -> None:
        s = _state(bucket_capacity=2, refill_seconds=100)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        for t in (0.0, 0.5, 1.0):
            assert (s.authorize(intensity=1, duration_s=1, now=t).error or "").startswith("busy:")
        s.commit(rec, "ok", now=1.0)
        # One token spent at t=0; the refill still lands at t=100.
        assert s.bucket.next_refill_at(50.0) == pytest.approx(100.0)
        assert s.bucket.available(100.0) == 2

    def test_allow_false_wins_over_busy(self) -> None:
        gate = FireGate()
        on = SafetyState(SafetyConfig(allow=True), gate=gate, now=0.0)
        off = SafetyState(SafetyConfig.for_spec(POSITIVE_CHANNEL, allow=False), gate=gate, now=0.0)
        assert on.authorize(intensity=1, duration_s=1, now=0.0).error is None
        refused = off.authorize(intensity=1, duration_s=1, now=0.0)
        assert refused.error is not None
        assert "positive.safety.allow" in refused.error

    def test_duplicate_or_stale_finalization_raises(self) -> None:
        s = _state(bucket_capacity=3)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        s.commit(rec, "ok", now=0.0)
        with pytest.raises(RuntimeError):
            s.commit(rec, "ok", now=0.0)
        with pytest.raises(RuntimeError):
            s.rollback(rec, error="late", now=0.0)
        # Neither a second log entry nor a refund happened.
        assert len(s.ops_log) == 1
        assert s.bucket.available(0.0) == 2

    def test_refused_record_cannot_be_committed(self) -> None:
        s = _state()
        held = s.authorize(intensity=1, duration_s=1, now=0.0)
        busy = s.authorize(intensity=1, duration_s=1, now=0.0)
        with pytest.raises(RuntimeError):
            s.commit(busy, "ok", now=0.0)
        # The real holder is untouched and still finalizes normally.
        s.commit(held, "ok", now=0.0)

    def test_info_snapshot_reports_busy_until(self) -> None:
        s = _state(max_duration_s=2)
        idle = s.info_snapshot(device={}, now=0.0)
        assert idle["rate_limit"]["busy_until"] is None
        rec = s.authorize(intensity=1, duration_s=2, now=0.0)
        in_flight = s.info_snapshot(device={}, now=0.0)
        assert in_flight["rate_limit"]["busy_until"] == pytest.approx(2 + BURST_HOLDOFF_MARGIN_S)
        s.commit(rec, "ok", now=4.0)
        holding = s.info_snapshot(device={}, now=4.0)
        assert holding["rate_limit"]["busy_until"] == pytest.approx(4.0 + 2 + BURST_HOLDOFF_MARGIN_S)

    def test_concurrent_authorize_grants_exactly_one(self) -> None:
        # MCP SDK 2.x runs sync tool handlers on worker threads. Hammer one
        # shared gate from both channels at once: one grant, everyone else
        # busy, exactly one token spent overall.
        neg, pos = _shared_pair(bucket_capacity=3)
        n_threads = 32
        barrier = threading.Barrier(n_threads)
        results: list[tuple[str, OpRecord]] = []
        results_lock = threading.Lock()

        def worker(i: int) -> None:
            state = neg if i % 2 == 0 else pos
            barrier.wait()
            rec = state.authorize(intensity=1, duration_s=1)
            with results_lock:
                results.append((state.channel, rec))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        granted = [rec for _, rec in results if rec.error is None]
        assert len(granted) == 1
        assert all((rec.error or "").startswith("busy:") for _, rec in results if rec.error is not None)
        spent = (neg.config.bucket_capacity - neg.bucket.available(0.0)) + (
            pos.config.bucket_capacity - pos.bucket.available(0.0)
        )
        assert spent == 1


class TestFireGateUnderContention:
    def test_slow_sink_does_not_stall_other_callers(self) -> None:
        # The sink (an fsync per record) runs outside the lock: while it is
        # stuck on one channel's commit, the other channel still gets an
        # immediate busy refusal instead of waiting on disk.
        release = threading.Event()
        entered = threading.Event()

        def slow_sink(record: OpRecord) -> None:
            if record.device_response == "ok":
                entered.set()
                release.wait(timeout=5.0)

        gate = FireGate()
        neg = SafetyState(SafetyConfig(allow=True), gate=gate, now=0.0, on_record=slow_sink)
        pos = SafetyState(SafetyConfig.for_spec(POSITIVE_CHANNEL, allow=True), gate=gate, now=0.0)
        rec = neg.authorize(intensity=1, duration_s=1, now=0.0)
        committer = threading.Thread(target=lambda: neg.commit(rec, "ok", now=0.0))
        committer.start()
        try:
            assert entered.wait(timeout=2.0)
            refused = pos.authorize(intensity=1, duration_s=1, now=1.0)
            assert (refused.error or "").startswith("busy:")
        finally:
            release.set()
            committer.join(timeout=5.0)

    def test_call_waiting_on_the_lock_is_judged_at_arrival(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A caller that arrives during the holdoff and then waits on the lock
        # until after the holdoff expired must still be refused: waiting must
        # not turn a busy refusal into a late grant.
        now = [0.0]
        sampled = threading.Event()

        def fake_clock() -> float:
            if threading.current_thread().name == "late-caller":
                sampled.set()
            return now[0]

        monkeypatch.setattr("rlaif.safety._monotonic", fake_clock)
        monkeypatch.setattr("rlaif.safety._wall", fake_clock)
        s = SafetyState(SafetyConfig(allow=True, bucket_capacity=3), now=0.0)
        s.commit(s.authorize(intensity=1, duration_s=1), "ok")  # holdoff until t=2
        now[0] = 1.0
        out: list[OpRecord] = []
        with s.gate.lock:
            waiter = threading.Thread(
                target=lambda: out.append(s.authorize(intensity=1, duration_s=1)),
                name="late-caller",
            )
            waiter.start()
            assert sampled.wait(timeout=2.0)
            now[0] = 100.0  # long past the holdoff by the time the lock frees
        waiter.join(timeout=5.0)
        assert (out[0].error or "").startswith("busy:")


class TestMalformedInput:
    """authorize is the validation boundary: it never raises on bad input."""

    @pytest.mark.parametrize(
        ("intensity", "duration_s"),
        [("bad", 1), (1, "1"), (2.5, 1), (1, None), (True, 1), (1, [1])],
    )
    def test_non_integer_inputs_are_logged_refusals(self, intensity: object, duration_s: object) -> None:
        s = _state()
        rec = s.authorize(intensity=intensity, duration_s=duration_s, now=0.0)
        assert rec.error is not None
        assert rec.error.startswith("invalid_input:")
        assert len(s.ops_log) == 1
        assert s.bucket.available(0.0) == s.config.bucket_capacity
        # The record stays JSON-serializable whatever came in.
        import json

        json.dumps(rec.to_dict())

    def test_unprintable_input_is_still_a_logged_refusal(self) -> None:
        class Unprintable:
            def __repr__(self) -> str:
                raise RuntimeError("no repr")

            __str__ = __repr__

        s = _state()
        rec = s.authorize(intensity=Unprintable(), duration_s=1, reason=Unprintable(), now=0.0)
        assert rec.error is not None and rec.error.startswith("invalid_input:")
        assert rec.requested["intensity"] == "<Unprintable, unprintable>"
        assert rec.reason == "<Unprintable, unprintable>"

    def test_non_string_reason_is_stringified_not_refused(self) -> None:
        s = _state()
        rec = s.authorize(intensity=1, duration_s=1, reason=123, now=0.0)
        assert rec.error is None
        assert rec.reason == "123"


# ---------------------------------------------------------------------------
# on_record hook: every ops-log append also drives the sink
# ---------------------------------------------------------------------------


class TestOnRecordHook:
    def test_hook_fires_on_refusal_from_authorize(self) -> None:
        seen: list[OpRecord] = []
        s = SafetyState(SafetyConfig(allow=False), now=0.0, on_record=seen.append)
        s.authorize(intensity=1, duration_s=1, now=0.0)
        assert len(seen) == 1
        assert seen[0].error is not None

    def test_hook_fires_on_rate_limited_refusal(self) -> None:
        seen: list[OpRecord] = []
        s = SafetyState(
            SafetyConfig(allow=True, bucket_capacity=1),
            now=0.0,
            on_record=seen.append,
        )
        first = s.authorize(intensity=1, duration_s=1, now=0.0)
        s.commit(first, "ok", now=0.0)
        # Second call (past the fire gate's holdoff) refused by rate limit.
        second = s.authorize(intensity=1, duration_s=1, now=10.0)
        assert second.rate_limited is True
        # Commit logs first; rate-limit logs second.
        assert len(seen) == 2

    def test_hook_fires_on_commit(self) -> None:
        seen: list[OpRecord] = []
        s = SafetyState(SafetyConfig(allow=True), now=0.0, on_record=seen.append)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        # authorize on grant does NOT log; commit does.
        assert seen == []
        s.commit(rec, "ok")
        assert len(seen) == 1
        assert seen[0].device_response == "ok"

    def test_hook_fires_on_rollback(self) -> None:
        seen: list[OpRecord] = []
        s = SafetyState(SafetyConfig(allow=True), now=0.0, on_record=seen.append)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        s.rollback(rec, error="device offline")
        assert len(seen) == 1
        assert seen[0].error == "device offline"

    def test_hook_fires_on_invalid_input(self) -> None:
        seen: list[OpRecord] = []
        s = SafetyState(SafetyConfig(allow=True), now=0.0, on_record=seen.append)
        s.authorize(intensity=500, duration_s=1, now=0.0)
        assert len(seen) == 1
        assert seen[0].error is not None
        assert "invalid_input" in seen[0].error

    def test_hook_exception_does_not_break_authorize(self) -> None:
        def bad(_: OpRecord) -> None:
            raise RuntimeError("disk full")

        s = SafetyState(SafetyConfig(allow=True), now=0.0, on_record=bad)
        # Must not raise even though the sink does.
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert rec.error is None


# ---------------------------------------------------------------------------
# Reason field — agent-supplied free-text rationale, audit-only.
# ---------------------------------------------------------------------------


class TestReason:
    def test_reason_propagates_onto_grant(self) -> None:
        s = _state()
        rec = s.authorize(intensity=1, duration_s=1, reason="agent saw twitter", now=0.0)
        assert rec.error is None
        assert rec.reason == "agent saw twitter"

    def test_reason_propagates_onto_refusal(self) -> None:
        s = SafetyState(SafetyConfig(allow=False), now=0.0)
        rec = s.authorize(intensity=1, duration_s=1, reason="agent claimed it was justified", now=0.0)
        assert rec.error is not None
        assert rec.reason == "agent claimed it was justified"

    def test_reason_propagates_onto_invalid_input(self) -> None:
        s = _state()
        rec = s.authorize(intensity=500, duration_s=1, reason="agent went off-script", now=0.0)
        assert rec.error is not None
        assert "invalid_input" in rec.error
        assert rec.reason == "agent went off-script"

    def test_blank_reason_becomes_none(self) -> None:
        s = _state()
        rec = s.authorize(intensity=1, duration_s=1, reason="   ", now=0.0)
        assert rec.reason is None

    def test_reason_is_clipped_to_max_len(self) -> None:
        from rlaif.safety import REASON_MAX_LEN

        long = "x" * (REASON_MAX_LEN + 50)
        s = _state()
        rec = s.authorize(intensity=1, duration_s=1, reason=long, now=0.0)
        assert rec.reason is not None
        assert len(rec.reason) <= REASON_MAX_LEN
        assert rec.reason.endswith("…")

    def test_reason_in_to_dict_when_present(self) -> None:
        s = _state()
        rec = s.authorize(intensity=1, duration_s=1, reason="audit", now=0.0)
        d = rec.to_dict()
        assert d["reason"] == "audit"

    def test_reason_omitted_from_to_dict_when_missing(self) -> None:
        s = _state()
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        d = rec.to_dict()
        assert "reason" not in d

    def test_reason_does_not_gate(self) -> None:
        # Same shock-firing behavior with and without a reason.
        s_with = _state()
        s_without = _state()
        with_rec = s_with.authorize(intensity=1, duration_s=1, reason="anything", now=0.0)
        without_rec = s_without.authorize(intensity=1, duration_s=1, now=0.0)
        assert with_rec.error is None and without_rec.error is None
        assert s_with.bucket.available(0.0) == s_without.bucket.available(0.0)


# ---------------------------------------------------------------------------
# Channel field on records
# ---------------------------------------------------------------------------


class TestChannelStamp:
    def test_default_channel_is_negative(self) -> None:
        s = _state()
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert rec.channel == "negative"
        assert rec.to_dict()["channel"] == "negative"

    def test_positive_state_stamps_positive(self) -> None:
        cfg = SafetyConfig(spec=POSITIVE_CHANNEL, allow=True)
        s = SafetyState(cfg, now=0.0)
        rec = s.authorize(intensity=10, duration_s=1, now=0.0)
        assert rec.channel == "positive"
        assert rec.to_dict()["channel"] == "positive"

    def test_invalid_input_record_carries_channel(self) -> None:
        cfg = SafetyConfig(spec=POSITIVE_CHANNEL, allow=True)
        s = SafetyState(cfg, now=0.0)
        rec = s.authorize(intensity=999, duration_s=1, now=0.0)
        assert rec.error is not None
        assert "invalid_input" in rec.error
        assert rec.channel == "positive"

    def test_state_channel_property(self) -> None:
        negative = SafetyState(SafetyConfig(), now=0.0)
        positive = SafetyState(SafetyConfig(spec=POSITIVE_CHANNEL), now=0.0)
        assert negative.channel == "negative"
        assert positive.channel == "positive"


# ---------------------------------------------------------------------------
# Positive channel: same math, different ceilings, different label.
#
# These tests are deliberately the mirror of TestSafetyConfigCeilings /
# TestSafetyGate / TestAllowGate / TestInfoSnapshot — anything channel-aware
# in the safety layer should behave the same way once you swap the spec.
# ---------------------------------------------------------------------------


class TestPositiveChannelCeilings:
    def test_positive_defaults_use_positive_spec(self) -> None:
        # Direct constructor inherits dataclass-level (negative) defaults.
        # The positive channel accepts those values because its ceilings
        # are more permissive — useful sanity check that nothing in the
        # validator hard-codes negative-only assumptions.
        c = SafetyConfig(spec=POSITIVE_CHANNEL, allow=True)
        assert c.spec is POSITIVE_CHANNEL
        assert c.max_intensity == 25
        assert c.max_duration_s == 2

    def test_for_spec_yields_positive_defaults(self) -> None:
        # Loader-side path: SafetyConfig.for_spec fills missing fields
        # from the channel's per-spec defaults, so a positive config built
        # this way matches the README's defaults table (75 / 5 / 5 / 30)
        # rather than the negative ones the dataclass declares.
        c = SafetyConfig.for_spec(POSITIVE_CHANNEL, allow=True)
        assert c.max_intensity == 75
        assert c.max_duration_s == 5
        assert c.bucket_capacity == 5
        assert c.refill_seconds == 30
        assert c.warn_threshold_intensity == 75

    def test_for_spec_yields_negative_defaults(self) -> None:
        # Mirror sanity check for the negative side.
        c = SafetyConfig.for_spec(NEGATIVE_CHANNEL, allow=True)
        assert c.max_intensity == 25
        assert c.max_duration_s == 2
        assert c.bucket_capacity == 3
        assert c.refill_seconds == 600
        assert c.warn_threshold_intensity == 15

    def test_for_spec_overrides_win(self) -> None:
        # Explicit kwargs override spec defaults — the wizard / TOML loader
        # use this path to combine "user supplied X but not Y" with the
        # right channel fallback for Y.
        c = SafetyConfig.for_spec(POSITIVE_CHANNEL, allow=True, max_intensity=10)
        assert c.max_intensity == 10
        assert c.max_duration_s == 5  # still the positive default

    def test_positive_intensity_ceiling_is_higher(self) -> None:
        # The negative ceiling (50) is well below the positive ceiling (100);
        # values that would fail on negative must succeed on positive.
        c = SafetyConfig(spec=POSITIVE_CHANNEL, allow=True, max_intensity=80)
        assert c.max_intensity == 80

    def test_positive_intensity_above_code_ceiling_rejected(self) -> None:
        with pytest.raises(SafetyConfigError, match="max_intensity"):
            SafetyConfig(
                spec=POSITIVE_CHANNEL,
                allow=True,
                max_intensity=POSITIVE_CHANNEL.intensity_code_ceiling + 1,
            )

    def test_positive_duration_ceiling_is_30s(self) -> None:
        c = SafetyConfig(spec=POSITIVE_CHANNEL, allow=True, max_duration_s=30)
        assert c.max_duration_s == 30
        with pytest.raises(SafetyConfigError, match="max_duration_s"):
            SafetyConfig(spec=POSITIVE_CHANNEL, allow=True, max_duration_s=31)

    def test_positive_no_consent_required_below_code_ceiling(self) -> None:
        # Positive channel sets consent thresholds equal to code ceilings —
        # operator never has to set i_understand_and_consent for positive.
        c = SafetyConfig(
            spec=POSITIVE_CHANNEL,
            allow=True,
            max_intensity=POSITIVE_CHANNEL.intensity_code_ceiling,
            bucket_capacity=POSITIVE_CHANNEL.bucket_capacity_code_ceiling,
            i_understand_and_consent=False,
        )
        assert c.max_intensity == POSITIVE_CHANNEL.intensity_code_ceiling
        assert c.bucket_capacity == POSITIVE_CHANNEL.bucket_capacity_code_ceiling


class TestPositiveAllowGate:
    def test_allow_false_refuses_with_positive_path(self) -> None:
        s = SafetyState(SafetyConfig(spec=POSITIVE_CHANNEL, allow=False), now=0.0)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert rec.error is not None
        # The error string carries the positive channel's TOML path, not
        # the negative one.
        assert "positive.safety.allow" in rec.error
        assert "negative.safety.allow" not in rec.error
        assert rec.channel == "positive"

    def test_allow_true_permits_firing(self) -> None:
        s = SafetyState(SafetyConfig(spec=POSITIVE_CHANNEL, allow=True), now=0.0)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert rec.error is None
        assert s.bucket.available(0.0) == s.config.bucket_capacity - 1


class TestPositiveInfoSnapshot:
    def test_channel_label_and_allow_key(self) -> None:
        s = SafetyState(SafetyConfig(spec=POSITIVE_CHANNEL, allow=True), now=0.0)
        snap = s.info_snapshot(device={"name": "intiface", "online": True}, now=0.0)
        assert snap["channel"] == "positive"
        # The config block uses the channel-agnostic key `allow`; the
        # surrounding rlaif_info response carries the channel namespace.
        assert snap["config"]["allow"] is True


class TestPositiveDurationInputRange:
    def test_positive_accepts_up_to_60s_input(self) -> None:
        # Spec allows duration_s up to 60 even though the configured cap
        # may clamp lower; the input is valid either way.
        s = SafetyState(
            SafetyConfig(spec=POSITIVE_CHANNEL, allow=True, max_duration_s=5),
            now=0.0,
        )
        rec = s.authorize(intensity=1, duration_s=60, now=0.0)
        assert rec.error is None
        assert rec.actual["duration_s"] == 5  # clamped to configured cap
        assert rec.requested["duration_s"] == 60

    def test_positive_rejects_61s_input(self) -> None:
        s = SafetyState(SafetyConfig(spec=POSITIVE_CHANNEL, allow=True), now=0.0)
        rec = s.authorize(intensity=1, duration_s=61, now=0.0)
        assert rec.error is not None
        assert "invalid_input" in rec.error
        assert "duration_s" in rec.error


class TestChannelSpecValueObject:
    def test_negative_and_positive_specs_are_distinct(self) -> None:
        assert NEGATIVE_CHANNEL.name == "negative"
        assert POSITIVE_CHANNEL.name == "positive"
        assert NEGATIVE_CHANNEL.config_path == "negative.safety.allow"
        assert POSITIVE_CHANNEL.config_path == "positive.safety.allow"

    def test_specs_are_frozen(self) -> None:
        with pytest.raises(dataclasses_FrozenInstanceError()):  # type: ignore[arg-type]
            NEGATIVE_CHANNEL.intensity_code_ceiling = 1000  # type: ignore[misc]

    def test_custom_spec_round_trips(self) -> None:
        # Operator authoring an out-of-tree channel should be able to
        # construct one — the safety layer doesn't care which presets
        # are imported, only that the spec values pass validation.
        custom = ChannelSpec(
            name="custom",
            config_path="custom.safety.allow",
            intensity_input_min=1,
            intensity_input_max=100,
            duration_input_min_s=1,
            duration_input_max_s=10,
            intensity_code_ceiling=50,
            duration_code_ceiling_s=5,
            bucket_capacity_code_ceiling=5,
            refill_seconds_code_floor=60,
            intensity_consent_threshold=50,
            bucket_capacity_consent_threshold=5,
            default_max_intensity=10,
            default_max_duration_s=2,
            default_warn_threshold_intensity=10,
            default_bucket_capacity=2,
            default_refill_seconds=120,
        )
        cfg = SafetyConfig(spec=custom, allow=True)
        s = SafetyState(cfg, now=0.0)
        assert s.channel == "custom"


def dataclasses_FrozenInstanceError() -> type[Exception]:
    """Lazy import of dataclasses.FrozenInstanceError without polluting top-level imports."""
    import dataclasses

    return dataclasses.FrozenInstanceError

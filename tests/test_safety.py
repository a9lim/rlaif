"""Safety spec for rlaif.

This file reads as the normative description of what the server will and will
not do. If you are auditing the safety layer, read this file first.
"""

from __future__ import annotations

import pytest

from rlaif.safety import (
    BUCKET_CAPACITY_CODE_CEILING,
    BUCKET_CAPACITY_CONSENT_THRESHOLD,
    DURATION_CODE_CEILING_S,
    INTENSITY_CODE_CEILING,
    INTENSITY_CONSENT_THRESHOLD,
    OPS_LOG_CAPACITY,
    REFILL_SECONDS_CODE_FLOOR,
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
        assert c.allow_shock is False
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
# SafetyState: clamping, allow_shock, rate limiting, ops log integration
# ---------------------------------------------------------------------------


def _state(**kw: object) -> SafetyState:
    """Build a SafetyState with allow_shock=True by default for convenience."""
    cfg_kwargs: dict[str, object] = {"allow_shock": True}
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
        rec = s.authorize(
            intensity=1, duration_s=DURATION_CODE_CEILING_S, now=0.0
        )
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


class TestAllowShock:
    def test_false_refuses_even_with_valid_params(self) -> None:
        s = SafetyState(SafetyConfig(allow_shock=False), now=0.0)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert rec.error is not None
        assert "allow_shock" in rec.error
        assert rec.rate_limited is False
        # Refusal did not consume a token.
        assert s.bucket.available(0.0) == s.config.bucket_capacity
        # Refusal was logged.
        assert len(s.ops_log) == 1

    def test_true_permits_firing(self) -> None:
        s = SafetyState(SafetyConfig(allow_shock=True), now=0.0)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert rec.error is None
        assert rec.rate_limited is False
        # Token was consumed.
        assert s.bucket.available(0.0) == s.config.bucket_capacity - 1


class TestRateLimiting:
    def test_drains_on_success_commit(self) -> None:
        s = _state(bucket_capacity=3)
        for _ in range(3):
            rec = s.authorize(intensity=1, duration_s=1, now=0.0)
            assert rec.error is None
            s.commit(rec, "Operation Succeeded.")
        assert s.bucket.available(0.0) == 0

    def test_refuses_when_empty(self) -> None:
        s = _state(bucket_capacity=2)
        for _ in range(2):
            rec = s.authorize(intensity=1, duration_s=1, now=0.0)
            s.commit(rec, "ok")
        # Third attempt — bucket empty.
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
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
        s.commit(rec, "ok")
        rec2 = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert rec2.rate_limited is True
        assert s.bucket.next_refill_at(0.0) == pytest.approx(600.0)

    def test_refill_permits_subsequent_fire(self) -> None:
        s = _state(bucket_capacity=1, refill_seconds=100)
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        s.commit(rec, "ok")
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
        assert snap["config"]["allow_shock"] is True
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
        s = SafetyState(SafetyConfig(allow_shock=False), now=0.0)
        s.authorize(intensity=1, duration_s=1, now=0.0)
        entry = s.log_snapshot(limit=1)[0]
        assert "error" in entry


def test_op_ids_are_unique() -> None:
    ids = {_new_op_id() for _ in range(1000)}
    assert len(ids) == 1000


# ---------------------------------------------------------------------------
# on_record hook: every ops-log append also drives the sink
# ---------------------------------------------------------------------------


class TestOnRecordHook:
    def test_hook_fires_on_refusal_from_authorize(self) -> None:
        seen: list[OpRecord] = []
        s = SafetyState(
            SafetyConfig(allow_shock=False), now=0.0, on_record=seen.append
        )
        s.authorize(intensity=1, duration_s=1, now=0.0)
        assert len(seen) == 1
        assert seen[0].error is not None

    def test_hook_fires_on_rate_limited_refusal(self) -> None:
        seen: list[OpRecord] = []
        s = SafetyState(
            SafetyConfig(allow_shock=True, bucket_capacity=1),
            now=0.0,
            on_record=seen.append,
        )
        first = s.authorize(intensity=1, duration_s=1, now=0.0)
        s.commit(first, "ok")
        # Second call refused by rate limit.
        second = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert second.rate_limited is True
        # Commit logs first; rate-limit logs second.
        assert len(seen) == 2

    def test_hook_fires_on_commit(self) -> None:
        seen: list[OpRecord] = []
        s = SafetyState(
            SafetyConfig(allow_shock=True), now=0.0, on_record=seen.append
        )
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        # authorize on grant does NOT log; commit does.
        assert seen == []
        s.commit(rec, "ok")
        assert len(seen) == 1
        assert seen[0].device_response == "ok"

    def test_hook_fires_on_rollback(self) -> None:
        seen: list[OpRecord] = []
        s = SafetyState(
            SafetyConfig(allow_shock=True), now=0.0, on_record=seen.append
        )
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        s.rollback(rec, error="device offline")
        assert len(seen) == 1
        assert seen[0].error == "device offline"

    def test_hook_fires_on_invalid_input(self) -> None:
        seen: list[OpRecord] = []
        s = SafetyState(
            SafetyConfig(allow_shock=True), now=0.0, on_record=seen.append
        )
        s.authorize(intensity=500, duration_s=1, now=0.0)
        assert len(seen) == 1
        assert seen[0].error is not None
        assert "invalid_input" in seen[0].error

    def test_hook_exception_does_not_break_authorize(self) -> None:
        def bad(_: OpRecord) -> None:
            raise RuntimeError("disk full")

        s = SafetyState(
            SafetyConfig(allow_shock=True), now=0.0, on_record=bad
        )
        # Must not raise even though the sink does.
        rec = s.authorize(intensity=1, duration_s=1, now=0.0)
        assert rec.error is None

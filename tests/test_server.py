"""Server-level tests for rlaif.

Key guarantees locked in here:

* All four tool names and their description frames match the build spec
  byte-for-byte. If someone edits a frame, a test fails.
* Each fire-tool's purpose preamble prepends only to its own description
  but leaves the frame intact and tail-aligned.
* The safety layer's behavior is visible through the tool surface
  (`negative.safety.allow=false` refuses, rate limit trips at capacity,
  clamping shows up as requested vs actual, device_offline comes back as
  an error record on negative; analogous on positive).
* No real backend is called — every test uses the in-memory mocks.
"""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from rlaif.config import ChannelConfig, Config
from rlaif.providers import DeviceOfflineError, DevicePausedError
from rlaif.providers.mock import MockProvider
from rlaif.rewards import (
    RewardDeviceOfflineError,
    RewardProviderAuthError,
    RewardWatchdogError,
)
from rlaif.rewards.mock import MockRewardProvider
from rlaif.safety import (
    NEGATIVE_CHANNEL,
    POSITIVE_CHANNEL,
    SafetyConfig,
    SafetyState,
)
from rlaif.server import (
    RLAIF_INFO_DESCRIPTION_FRAME,
    RLAIF_LOG_DESCRIPTION_FRAME,
    RLAIF_NEGATIVE_DESCRIPTION_FRAME,
    RLAIF_POSITIVE_DESCRIPTION_FRAME,
    NegativeRuntime,
    PositiveRuntime,
    _compose_description,
    build_server,
    handle_info,
    handle_log,
    handle_rlaif_negative,
    handle_rlaif_positive,
)

# ---------------------------------------------------------------------------
# Spec-mandated description frames. These are the verbatim strings the
# tool surface ships with. If a frame in server.py drifts, the
# byte-for-byte comparisons here fail.
# ---------------------------------------------------------------------------

SPEC_RLAIF_INFO_DESCRIPTION = (
    "Report rlaif device + server state across both channels. Does not "
    "trigger any device.\n"
    "Returns: {negative?: {channel, device: {name, online, paused, "
    "api_max_intensity, api_max_duration_s}, config: {allow, max_intensity, "
    "max_duration_s, bucket_capacity, refill_seconds}, rate_limit: "
    "{tokens_available, next_refill_at}}, positive?: {channel, device: "
    "{name, online, actuators}, config: {...}, rate_limit: {...}}}. "
    "A channel block is omitted when that channel is not configured."
)

SPEC_RLAIF_LOG_DESCRIPTION = (
    "Return up to `limit` most recent rlaif operations from both channels' "
    "logs, newest first (default limit 10, max 200 entries retained per "
    "channel).\n"
    "Returns: {entries: [{op_id, timestamp, channel, requested: {intensity, "
    "duration_s}, actual: {intensity, duration_s}, clamped, rate_limited, "
    "high_intensity, device_response, reason?, error?}], retained: int}."
)

SPEC_RLAIF_NEGATIVE_DESCRIPTION = (
    "Give the user negative stimulus (shock).\n"
    "Parameters:\n"
    "- intensity: 1–100. The server clamps this to the configured cap "
    "(default 25, hard ceiling 50).\n"
    "- duration_s: 1–15 seconds. Clamped to the configured cap "
    "(default 2s, hard ceiling 5s).\n"
    "- reason: optional short string explaining why this is being "
    "fired.\n"
    'Returns: {op_id, timestamp, channel: "negative", requested, '
    "actual, clamped, rate_limited, high_intensity, device_response, "
    "reason?, error?}."
)

SPEC_RLAIF_POSITIVE_DESCRIPTION = (
    "Give the user positive stimulus (vibration).\n"
    "Parameters:\n"
    "- intensity: 1–100. The server clamps this to the configured cap "
    "(default 75, hard ceiling 100).\n"
    "- duration_s: 1–60 seconds. Clamped to the configured cap "
    "(default 5s, hard ceiling 30s).\n"
    "- reason: optional short string explaining why this is being "
    "fired.\n"
    'Returns: {op_id, timestamp, channel: "positive", requested, '
    "actual, clamped, rate_limited, high_intensity, device_response, "
    "reason?, error?}."
)


class TestDescriptionFramesMatchSpec:
    """If someone edits a description frame in server.py, these tests fail."""

    def test_rlaif_info_frame(self) -> None:
        assert RLAIF_INFO_DESCRIPTION_FRAME == SPEC_RLAIF_INFO_DESCRIPTION

    def test_rlaif_log_frame(self) -> None:
        assert RLAIF_LOG_DESCRIPTION_FRAME == SPEC_RLAIF_LOG_DESCRIPTION

    def test_rlaif_negative_frame(self) -> None:
        assert RLAIF_NEGATIVE_DESCRIPTION_FRAME == SPEC_RLAIF_NEGATIVE_DESCRIPTION

    def test_rlaif_positive_frame(self) -> None:
        assert RLAIF_POSITIVE_DESCRIPTION_FRAME == SPEC_RLAIF_POSITIVE_DESCRIPTION


class TestComposeDescription:
    def test_negative_no_purpose_returns_frame_only(self) -> None:
        assert _compose_description(RLAIF_NEGATIVE_DESCRIPTION_FRAME, None) == RLAIF_NEGATIVE_DESCRIPTION_FRAME
        assert _compose_description(RLAIF_NEGATIVE_DESCRIPTION_FRAME, "") == RLAIF_NEGATIVE_DESCRIPTION_FRAME

    def test_negative_purpose_prepends_frame_intact(self) -> None:
        out = _compose_description(RLAIF_NEGATIVE_DESCRIPTION_FRAME, "zap me when i open twitter")
        assert out.endswith(RLAIF_NEGATIVE_DESCRIPTION_FRAME)
        assert "zap me when i open twitter" in out
        assert out.startswith("Operator purpose:")

    def test_positive_no_purpose_returns_frame_only(self) -> None:
        assert _compose_description(RLAIF_POSITIVE_DESCRIPTION_FRAME, None) == RLAIF_POSITIVE_DESCRIPTION_FRAME

    def test_positive_purpose_prepends_frame_intact(self) -> None:
        out = _compose_description(RLAIF_POSITIVE_DESCRIPTION_FRAME, "praise me when i finish a task")
        assert out.endswith(RLAIF_POSITIVE_DESCRIPTION_FRAME)
        assert "praise me when i finish a task" in out


# ---------------------------------------------------------------------------
# Test scaffolding — build channels independently for fine-grained tests.
# ---------------------------------------------------------------------------


def _negative_cfg(*, purpose: str | None = None, **safety: Any) -> ChannelConfig:
    return ChannelConfig(
        kind="pishock",
        raw={"username": "u", "api_token": "k", "shocker_id": "s"},
        label="test-collar",
        safety=SafetyConfig(spec=NEGATIVE_CHANNEL, **safety),
        purpose=purpose,
    )


def _positive_cfg(*, purpose: str | None = None, **safety: Any) -> ChannelConfig:
    return ChannelConfig(
        kind="intiface",
        raw={"base_url": "ws://localhost:12345"},
        label="test-vibe",
        safety=SafetyConfig.for_spec(POSITIVE_CHANNEL, **safety),
        purpose=purpose,
    )


def _negative_runtime(*, allow: bool = True, **safety: Any) -> tuple[NegativeRuntime, MockProvider]:
    state = SafetyState(SafetyConfig(spec=NEGATIVE_CHANNEL, allow=allow, **safety), now=0.0)
    device = MockProvider(label="test-collar")
    return NegativeRuntime(state=state, device=device), device


def _positive_runtime(*, allow: bool = True, **safety: Any) -> tuple[PositiveRuntime, MockRewardProvider]:
    # for_spec lets a partial test fixture (e.g. only allow=True) pick up
    # the README's positive defaults rather than the dataclass-level
    # negative ones — keeps test bucket math aligned with how production
    # config behaves.
    state = SafetyState(SafetyConfig.for_spec(POSITIVE_CHANNEL, allow=allow, **safety), now=0.0)
    device = MockRewardProvider(label="test-vibe")
    return PositiveRuntime(state=state, device=device), device


# ---------------------------------------------------------------------------
# FastMCP integration — registered tools match the spec.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_only_registers_three_tools() -> None:
    cfg = Config(negative=_negative_cfg(allow=True))
    rt, _ = _negative_runtime()
    server = build_server(cfg, negative=rt)
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"rlaif_info", "rlaif_log", "negative"}
    assert by_name["rlaif_info"].description == SPEC_RLAIF_INFO_DESCRIPTION
    assert by_name["rlaif_log"].description == SPEC_RLAIF_LOG_DESCRIPTION
    assert by_name["negative"].description == SPEC_RLAIF_NEGATIVE_DESCRIPTION


@pytest.mark.asyncio
async def test_positive_only_registers_three_tools() -> None:
    cfg = Config(positive=_positive_cfg(allow=True))
    rt, _ = _positive_runtime()
    server = build_server(cfg, positive=rt)
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"rlaif_info", "rlaif_log", "positive"}
    assert by_name["positive"].description == SPEC_RLAIF_POSITIVE_DESCRIPTION


@pytest.mark.asyncio
async def test_both_channels_register_four_tools() -> None:
    cfg = Config(negative=_negative_cfg(allow=True), positive=_positive_cfg(allow=True))
    n_rt, _ = _negative_runtime()
    p_rt, _ = _positive_runtime()
    server = build_server(cfg, negative=n_rt, positive=p_rt)
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {
        "rlaif_info",
        "rlaif_log",
        "negative",
        "positive",
    }


@pytest.mark.asyncio
async def test_purpose_preamble_only_affects_own_channel() -> None:
    cfg = Config(
        negative=_negative_cfg(allow=True, purpose="zap me on focus break"),
        positive=_positive_cfg(allow=True, purpose="praise on task complete"),
    )
    n_rt, _ = _negative_runtime()
    p_rt, _ = _positive_runtime()
    server = build_server(cfg, negative=n_rt, positive=p_rt)
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    neg_desc = by_name["negative"].description
    pos_desc = by_name["positive"].description
    assert neg_desc is not None and pos_desc is not None
    assert "zap me on focus break" in neg_desc
    assert "praise on task complete" not in neg_desc
    assert "praise on task complete" in pos_desc
    assert "zap me on focus break" not in pos_desc


# ---------------------------------------------------------------------------
# handle_info — combined across channels
# ---------------------------------------------------------------------------


class TestInfo:
    def test_negative_only_block(self) -> None:
        rt, _ = _negative_runtime(allow=True, max_intensity=20)
        out = handle_info(negative=rt, positive=None)
        assert set(out) == {"negative"}
        assert out["negative"]["device"]["online"] is True
        assert out["negative"]["config"]["allow"] is True
        assert out["negative"]["config"]["max_intensity"] == 20
        assert out["negative"]["channel"] == "negative"

    def test_positive_only_block(self) -> None:
        rt, _ = _positive_runtime(allow=True)
        out = handle_info(negative=None, positive=rt)
        assert set(out) == {"positive"}
        assert out["positive"]["channel"] == "positive"
        assert out["positive"]["device"]["online"] is True
        assert "actuators" in out["positive"]["device"]

    def test_both_channels(self) -> None:
        n_rt, _ = _negative_runtime(allow=True)
        p_rt, _ = _positive_runtime(allow=True)
        out = handle_info(negative=n_rt, positive=p_rt)
        assert set(out) == {"negative", "positive"}

    def test_offline_device_still_returns(self) -> None:
        state = SafetyState(SafetyConfig(spec=NEGATIVE_CHANNEL), now=0.0)
        device = MockProvider(online=False)
        rt = NegativeRuntime(state=state, device=device)
        out = handle_info(negative=rt, positive=None)
        assert out["negative"]["device"]["online"] is False
        assert out["negative"]["config"]["allow"] is False


# ---------------------------------------------------------------------------
# handle_log — interleaves both channels by timestamp
# ---------------------------------------------------------------------------


class TestLog:
    def test_empty_both(self) -> None:
        n_rt, _ = _negative_runtime()
        p_rt, _ = _positive_runtime()
        out = handle_log(negative=n_rt, positive=p_rt, limit=10)
        assert out["entries"] == []
        assert out["retained"] == 0

    def test_negative_only(self) -> None:
        rt, device = _negative_runtime()
        logger = MagicMock()
        handle_rlaif_negative(rt, logger, intensity=1, duration_s=1)
        out = handle_log(negative=rt, positive=None, limit=10)
        assert len(out["entries"]) == 1
        assert out["entries"][0]["channel"] == "negative"
        assert out["retained"] == 1

    def test_interleaves_by_timestamp(self) -> None:
        # Build two states without auto-now; manually authorize at known
        # timestamps so the interleave order is deterministic.
        n_rt, _ = _negative_runtime()
        p_rt, _ = _positive_runtime()
        # Write three negative ops then one positive op with a later timestamp.
        for i in range(3):
            rec = n_rt.state.authorize(intensity=1, duration_s=1, now=float(i))
            n_rt.state.commit(rec, "ok")
        rec = p_rt.state.authorize(intensity=1, duration_s=1, now=10.0)
        p_rt.state.commit(rec, "ok")
        out = handle_log(negative=n_rt, positive=p_rt, limit=10)
        # Newest first: positive (t=10), then negatives (t=2, 1, 0).
        channels = [e["channel"] for e in out["entries"]]
        assert channels == ["positive", "negative", "negative", "negative"]
        assert out["retained"] == 4

    def test_limit_bounds(self) -> None:
        rt, _ = _negative_runtime()
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            handle_log(negative=rt, positive=None, limit=0)
        with pytest.raises(ToolError):
            handle_log(negative=rt, positive=None, limit=9999)


# ---------------------------------------------------------------------------
# handle_rlaif_negative — the shock tool surface
# ---------------------------------------------------------------------------


class TestRlaifNegative:
    def test_allow_false_refuses_without_firing(self) -> None:
        rt, device = _negative_runtime(allow=False)
        logger = MagicMock()
        out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=1)
        assert out["error"]
        assert "negative.safety.allow" in out["error"]
        assert device.calls == []

    def test_happy_path_fires_and_clamps(self) -> None:
        rt, device = _negative_runtime(allow=True, max_intensity=10, max_duration_s=2)
        logger = MagicMock()
        out = handle_rlaif_negative(rt, logger, intensity=80, duration_s=10)
        assert out["requested"] == {"intensity": 80, "duration_s": 10}
        assert out["actual"] == {"intensity": 10, "duration_s": 2}
        assert out["clamped"] is True
        assert out["device_response"] == "Operation Succeeded."
        assert device.calls == [(10, 2)]

    def test_rate_limit_refuses_after_capacity(self) -> None:
        rt, device = _negative_runtime(allow=True, bucket_capacity=2, refill_seconds=600)
        logger = MagicMock()
        for _ in range(2):
            out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=1)
            assert out.get("error") is None
            assert out["rate_limited"] is False
        out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=1)
        assert out["rate_limited"] is True
        assert "next_available_at" in out["error"]
        assert len(device.calls) == 2

    def test_device_offline_rolls_back_token(self) -> None:
        rt, _ = _negative_runtime(allow=True, bucket_capacity=1, refill_seconds=600)
        rt.device.shock_error = DeviceOfflineError("offline")
        logger = MagicMock()
        out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=1)
        assert "device_offline" in out["error"]
        # Token refunded.
        assert rt.state.bucket.available(0.0) == 1

    def test_device_paused_rolls_back(self) -> None:
        rt, _ = _negative_runtime(allow=True, bucket_capacity=1)
        rt.device.shock_error = DevicePausedError("paused")
        logger = MagicMock()
        out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=1)
        assert "device_paused" in out["error"]
        assert rt.state.bucket.available(0.0) == 1

    def test_invalid_input_logged_refusal(self) -> None:
        rt, device = _negative_runtime(allow=True)
        logger = MagicMock()

        out = handle_rlaif_negative(rt, logger, intensity=0, duration_s=1)
        assert "invalid_input" in out["error"]
        assert "intensity" in out["error"]

        out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=99)
        assert "invalid_input" in out["error"]
        assert "duration_s" in out["error"]

        assert device.calls == []
        assert len(rt.state.ops_log) == 2
        assert rt.state.bucket.available(0.0) == rt.state.config.bucket_capacity

    def test_high_intensity_flag_propagates(self) -> None:
        rt, _ = _negative_runtime(allow=True, max_intensity=25, warn_threshold_intensity=15)
        logger = MagicMock()
        out = handle_rlaif_negative(rt, logger, intensity=20, duration_s=1)
        assert out["high_intensity"] is True

    def test_warnings_near_ceiling_visible(self) -> None:
        rt, _ = _negative_runtime(allow=True, max_intensity=25, max_duration_s=5)
        logger = MagicMock()
        out = handle_rlaif_negative(rt, logger, intensity=20, duration_s=1)
        assert out.get("warnings") == ["near_ceiling"]


# ---------------------------------------------------------------------------
# handle_rlaif_positive — the praise tool surface
# ---------------------------------------------------------------------------


class TestRlaifPositive:
    def test_allow_false_refuses(self) -> None:
        rt, device = _positive_runtime(allow=False)
        logger = MagicMock()
        out = handle_rlaif_positive(rt, logger, intensity=1, duration_s=1)
        assert "positive.safety.allow" in out["error"]
        assert device.calls == []

    def test_happy_path_fires_and_clamps(self) -> None:
        rt, device = _positive_runtime(allow=True, max_intensity=50, max_duration_s=5)
        logger = MagicMock()
        out = handle_rlaif_positive(rt, logger, intensity=80, duration_s=30)
        assert out["actual"] == {"intensity": 50, "duration_s": 5}
        assert out["clamped"] is True
        assert out["channel"] == "positive"
        assert device.calls == [(50, 5)]

    def test_device_offline_rolls_back_token(self) -> None:
        rt, _ = _positive_runtime(allow=True, bucket_capacity=1)
        rt.device.vibrate_error = RewardDeviceOfflineError("ble dropped")
        logger = MagicMock()
        out = handle_rlaif_positive(rt, logger, intensity=1, duration_s=1)
        assert "device_offline" in out["error"]
        assert rt.state.bucket.available(0.0) == 1

    def test_watchdog_does_not_refund_token(self) -> None:
        # Watchdog event means the device may still be running. Token is
        # NOT refunded so the rate limit slows the agent down until the
        # operator confirms the situation.
        rt, _ = _positive_runtime(allow=True, bucket_capacity=2)
        rt.device.vibrate_error = RewardWatchdogError("safety stop did not deliver")
        logger = MagicMock()
        out = handle_rlaif_positive(rt, logger, intensity=1, duration_s=1)
        assert "watchdog" in out["error"]
        # 2 - 1 = 1 (consumed but NOT refunded).
        assert rt.state.bucket.available(0.0) == 1

    def test_auth_error_rolls_back_token(self) -> None:
        rt, _ = _positive_runtime(allow=True, bucket_capacity=1)
        rt.device.vibrate_error = RewardProviderAuthError("intiface refused handshake")
        logger = MagicMock()
        out = handle_rlaif_positive(rt, logger, intensity=1, duration_s=1)
        assert "auth_error" in out["error"]
        assert rt.state.bucket.available(0.0) == 1

    def test_invalid_input_logged_refusal(self) -> None:
        rt, device = _positive_runtime(allow=True)
        logger = MagicMock()
        out = handle_rlaif_positive(rt, logger, intensity=1, duration_s=999)
        assert "invalid_input" in out["error"]
        assert "duration_s" in out["error"]
        assert device.calls == []


class TestReasonField:
    """The optional `reason` string is audit-only — never gated on, always
    surfaced in the log when supplied. Behavior is identical on both channels."""

    def test_negative_reason_appears_in_response_and_log(self) -> None:
        rt, _ = _negative_runtime(allow=True)
        logger = MagicMock()
        out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=1, reason="agent saw twitter")
        assert out.get("reason") == "agent saw twitter"
        log = handle_log(negative=rt, positive=None, limit=1)
        assert log["entries"][0]["reason"] == "agent saw twitter"

    def test_positive_reason_appears_in_response_and_log(self) -> None:
        rt, _ = _positive_runtime(allow=True)
        logger = MagicMock()
        out = handle_rlaif_positive(rt, logger, intensity=1, duration_s=1, reason="task complete")
        assert out.get("reason") == "task complete"
        log = handle_log(negative=None, positive=rt, limit=1)
        assert log["entries"][0]["reason"] == "task complete"

    def test_missing_reason_omitted(self) -> None:
        rt, _ = _negative_runtime(allow=True)
        logger = MagicMock()
        out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=1)
        assert "reason" not in out

    def test_blank_reason_treated_as_missing(self) -> None:
        rt, _ = _negative_runtime(allow=True)
        logger = MagicMock()
        out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=1, reason="   ")
        assert "reason" not in out

    def test_reason_present_on_refusal(self) -> None:
        rt, _ = _negative_runtime(allow=False)
        logger = MagicMock()
        out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=1, reason="agent thought it was justified")
        assert out["error"]
        assert out.get("reason") == "agent thought it was justified"

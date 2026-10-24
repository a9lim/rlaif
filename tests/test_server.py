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
* One stimulus at a time across both channels, including when the SDK
  dispatches tool calls concurrently on worker threads.
* No real backend is called — every test uses the in-memory mocks.
"""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp import Client

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
    BURST_HOLDOFF_MARGIN_S,
    NEGATIVE_CHANNEL,
    POSITIVE_CHANNEL,
    FireGate,
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
    "{tokens_available, next_refill_at, busy_until}}, positive?: {channel, "
    "device: {name, online, actuators}, config: {...}, rate_limit: {...}}}. "
    "busy_until is when the next stimulus on either channel may fire, or "
    "null if one may fire now. A channel block is omitted when that "
    "channel is not configured."
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
    "One stimulus runs at a time across both channels: a call made while "
    'another is running or settling is refused with error "busy: …" and '
    "spends no token.\n"
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
    "One stimulus runs at a time across both channels: a call made while "
    'another is running or settling is refused with error "busy: …" and '
    "spends no token.\n"
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


def _negative_runtime(
    *, allow: bool = True, gate: FireGate | None = None, **safety: Any
) -> tuple[NegativeRuntime, MockProvider]:
    state = SafetyState(SafetyConfig(spec=NEGATIVE_CHANNEL, allow=allow, **safety), gate=gate, now=0.0)
    device = MockProvider(label="test-collar")
    return NegativeRuntime(state=state, device=device), device


def _positive_runtime(
    *, allow: bool = True, gate: FireGate | None = None, **safety: Any
) -> tuple[PositiveRuntime, MockRewardProvider]:
    # for_spec lets a partial test fixture (e.g. only allow=True) pick up
    # the README's positive defaults rather than the dataclass-level
    # negative ones — keeps test bucket math aligned with how production
    # config behaves.
    state = SafetyState(SafetyConfig.for_spec(POSITIVE_CHANNEL, allow=allow, **safety), gate=gate, now=0.0)
    device = MockRewardProvider(label="test-vibe")
    return PositiveRuntime(state=state, device=device), device


def _paired_runtimes() -> tuple[NegativeRuntime, PositiveRuntime]:
    """Both channels on one FireGate, the way ``main`` wires them."""
    gate = FireGate()
    n_rt, _ = _negative_runtime(gate=gate)
    p_rt, _ = _positive_runtime(gate=gate)
    return n_rt, p_rt


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Drive ``rlaif.safety``'s monotonic and wall clocks from one fake.

    The handlers never pass ``now``, so tests that fire through them more
    than once advance this past the fire gate's holdoff instead.
    """
    fake = _FakeClock()
    monkeypatch.setattr("rlaif.safety._monotonic", fake)
    monkeypatch.setattr("rlaif.safety._wall", fake)
    return fake


# ---------------------------------------------------------------------------
# MCPServer integration — registered tools match the spec.
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
    n_rt, p_rt = _paired_runtimes()
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
    n_rt, p_rt = _paired_runtimes()
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
        n_rt, p_rt = _paired_runtimes()
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
        # timestamps (spaced past the fire gate's holdoff) so the interleave
        # order is deterministic.
        n_rt, p_rt = _paired_runtimes()
        # Write three negative ops then one positive op with a later timestamp.
        for i in range(3):
            t = 10.0 * i
            rec = n_rt.state.authorize(intensity=1, duration_s=1, now=t)
            n_rt.state.commit(rec, "ok", now=t)
        rec = p_rt.state.authorize(intensity=1, duration_s=1, now=30.0)
        p_rt.state.commit(rec, "ok", now=30.0)
        out = handle_log(negative=n_rt, positive=p_rt, limit=10)
        # Newest first: positive (t=30), then negatives (t=20, 10, 0).
        channels = [e["channel"] for e in out["entries"]]
        assert channels == ["positive", "negative", "negative", "negative"]
        assert out["retained"] == 4

    def test_limit_bounds(self) -> None:
        rt, _ = _negative_runtime()
        from mcp.server.mcpserver.exceptions import ToolError

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

    def test_rate_limit_refuses_after_capacity(self, clock: _FakeClock) -> None:
        rt, device = _negative_runtime(allow=True, bucket_capacity=2, refill_seconds=600)
        logger = MagicMock()
        for _ in range(2):
            out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=1)
            assert out.get("error") is None
            assert out["rate_limited"] is False
            clock.t += 10.0
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


# ---------------------------------------------------------------------------
# Fire gate through the tool surface — one stimulus at a time, server-wide.
# ---------------------------------------------------------------------------


class TestFireGateSurface:
    def test_back_to_back_shock_is_busy_until_burst_settles(self, clock: _FakeClock) -> None:
        rt, device = _negative_runtime(allow=True, bucket_capacity=3, max_duration_s=2)
        logger = MagicMock()
        first = handle_rlaif_negative(rt, logger, intensity=1, duration_s=2)
        assert first.get("error") is None
        second = handle_rlaif_negative(rt, logger, intensity=1, duration_s=2)
        assert second["error"].startswith("busy:")
        assert device.calls == [(1, 2)]
        assert rt.state.bucket.available(clock.t) == 2  # refusal spent nothing
        clock.t += 2 + BURST_HOLDOFF_MARGIN_S
        third = handle_rlaif_negative(rt, logger, intensity=1, duration_s=2)
        assert third.get("error") is None
        assert device.calls == [(1, 2), (1, 2)]

    def test_shock_blocks_vibration_and_vice_versa(self, clock: _FakeClock) -> None:
        n_rt, p_rt = _paired_runtimes()
        logger = MagicMock()
        assert handle_rlaif_negative(n_rt, logger, intensity=1, duration_s=1).get("error") is None
        blocked = handle_rlaif_positive(p_rt, logger, intensity=10, duration_s=1)
        assert "negative stimulus is in progress" in blocked["error"]
        assert p_rt.device.calls == []

        clock.t += 1 + BURST_HOLDOFF_MARGIN_S
        assert handle_rlaif_positive(p_rt, logger, intensity=10, duration_s=1).get("error") is None
        # Vibration success confirms the stop, so only the margin holds.
        blocked = handle_rlaif_negative(n_rt, logger, intensity=1, duration_s=1)
        assert "positive stimulus is in progress" in blocked["error"]
        clock.t += BURST_HOLDOFF_MARGIN_S
        assert handle_rlaif_negative(n_rt, logger, intensity=1, duration_s=1).get("error") is None

    def test_failed_fire_still_holds_the_gate(self, clock: _FakeClock) -> None:
        # A timeout may have delivered the shock with the response lost.
        rt, _ = _negative_runtime(allow=True, bucket_capacity=3, max_duration_s=2)
        rt.device.shock_error = DeviceOfflineError("timeout")
        logger = MagicMock()
        out = handle_rlaif_negative(rt, logger, intensity=1, duration_s=2)
        assert "device_offline" in out["error"]
        rt.device.shock_error = None
        assert handle_rlaif_negative(rt, logger, intensity=1, duration_s=2)["error"].startswith("busy:")

    def test_build_server_rejects_unshared_gates(self) -> None:
        cfg = Config(negative=_negative_cfg(allow=True), positive=_positive_cfg(allow=True))
        n_rt, _ = _negative_runtime()
        p_rt, _ = _positive_runtime()
        with pytest.raises(ValueError, match="FireGate"):
            build_server(cfg, negative=n_rt, positive=p_rt)


class _SlowShocker(MockProvider):
    """Holds each shock for a while and records how many overlap."""

    def __init__(self) -> None:
        super().__init__(label="slow")
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.fired = 0

    def shock(self, *, intensity: int, duration_s: int) -> str:
        with self._lock:
            self.active += 1
            self.fired += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.2)
        with self._lock:
            self.active -= 1
        return "Operation Succeeded."


@pytest.mark.asyncio
async def test_concurrent_tool_calls_never_overlap_bursts() -> None:
    # Regression for the SDK 2.x port: sync handlers run on worker threads,
    # so parallel tool calls used to overlap on the device. Through the
    # real MCP dispatch path: exactly one fires, the rest come back busy.
    device = _SlowShocker()
    state = SafetyState(SafetyConfig(spec=NEGATIVE_CHANNEL, allow=True, bucket_capacity=3))
    server = build_server(
        Config(negative=_negative_cfg(allow=True, bucket_capacity=3)),
        negative=NegativeRuntime(state=state, device=device),
        logger=MagicMock(),
    )
    args = {"intensity": 1, "duration_s": 1}
    async with Client(server) as client:
        results = await asyncio.gather(*(client.call_tool("negative", args) for _ in range(5)))
    payloads = [r.structured_content for r in results]
    assert device.fired == 1
    assert device.max_active == 1
    assert sum(1 for p in payloads if p is not None and p.get("error") is None) == 1
    assert sum(1 for p in payloads if p is not None and str(p.get("error", "")).startswith("busy:")) == 4


@pytest.mark.asyncio
async def test_tool_annotations_mark_read_only_and_firing_tools() -> None:
    n_rt, p_rt = _paired_runtimes()
    cfg = Config(negative=_negative_cfg(allow=True), positive=_positive_cfg(allow=True))
    server = build_server(cfg, negative=n_rt, positive=p_rt, logger=MagicMock())
    by_name = {tool.name: tool for tool in await server.list_tools()}
    for name in ("rlaif_info", "rlaif_log"):
        ann = by_name[name].annotations
        assert ann is not None and ann.read_only_hint is True
    for name in ("negative", "positive"):
        ann = by_name[name].annotations
        assert ann is not None
        assert ann.read_only_hint is False
        assert ann.destructive_hint is True
        assert ann.idempotent_hint is False

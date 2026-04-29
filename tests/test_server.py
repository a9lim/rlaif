"""Server-level tests for rlaif.

Key guarantees locked in here:

* The three tool names and their description frames match the build spec
  byte-for-byte. If someone edits a frame, a test fails.
* The configurable purpose preamble prepends to the rlaif description but
  leaves the frame intact and tail-aligned.
* The safety layer's behavior is visible through the tool surface
  (allow_shock=false refuses, rate limit trips at capacity, clamping shows
  up as requested vs actual, device_offline comes back as an error record).
* No real backend is called — every test uses the in-memory MockProvider.
"""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from rlaif.config import Config, DeviceConfig, ProviderConfig, ToolConfig
from rlaif.providers import DeviceOfflineError, DevicePausedError
from rlaif.providers.mock import MockProvider
from rlaif.safety import SafetyConfig, SafetyState
from rlaif.server import (
    RLAIF_DESCRIPTION_FRAME,
    RLAIF_INFO_DESCRIPTION_FRAME,
    RLAIF_LOG_DESCRIPTION_FRAME,
    build_server,
    compose_rlaif_description,
    handle_info,
    handle_log,
    handle_rlaif,
)

# ---------------------------------------------------------------------------
# Spec-mandated description frames. These are the verbatim strings from the
# build prompt. If a frame in server.py drifts from the spec, these
# comparisons fail.
# ---------------------------------------------------------------------------

SPEC_RLAIF_INFO_DESCRIPTION = (
    "Report current shock device status and rlaif server state. Does not "
    "trigger the device.\n"
    "Returns: {device: {name, online, paused, "
    "api_max_intensity, api_max_duration_s}, config: {allow_shock, "
    "max_intensity, max_duration_s, bucket_capacity, refill_seconds}, "
    "rate_limit: {tokens_available, next_refill_at}}."
)

SPEC_RLAIF_LOG_DESCRIPTION = (
    "Return up to `limit` most recent rlaif operations from the log "
    "(default limit 10, max 200 entries retained).\n"
    "Returns: {op_id, timestamp, requested: {intensity, "
    "duration_s}, actual: {intensity, duration_s}, clamped, rate_limited, "
    "high_intensity, device_response, reason?, error?}."
)

SPEC_RLAIF_DESCRIPTION = (
    "Shock the user.\n"
    "Parameters:\n"
    "- intensity: 1–100. The server clamps this to the configured cap "
    "(default 25, hard ceiling 50).\n"
    "- duration_s: 1–15 seconds. Clamped to the configured cap "
    "(default 2s, hard ceiling 5s).\n"
    "- reason: optional short string explaining why this shock is being "
    "fired. Logged for the operator to review; never gates the decision.\n"
    "Rate limiting: default capacity 3, refill 1 token per 600s. "
    "Calls exceeding this do not fire.\n"
    "Hard refusal: if `allow_shock: false` in server config, all calls are "
    "refused.\n"
    "Returns: {op_id, timestamp, requested: {intensity, duration_s}, "
    "actual: {intensity, duration_s}, clamped: bool, rate_limited: bool, "
    "high_intensity: bool, device_response: str, reason?: str, "
    "error?: str}."
)


class TestDescriptionFramesMatchSpec:
    """If someone edits a description frame in server.py, these tests fail."""

    def test_rlaif_info_frame(self) -> None:
        assert RLAIF_INFO_DESCRIPTION_FRAME == SPEC_RLAIF_INFO_DESCRIPTION

    def test_rlaif_log_frame(self) -> None:
        assert RLAIF_LOG_DESCRIPTION_FRAME == SPEC_RLAIF_LOG_DESCRIPTION

    def test_rlaif_frame(self) -> None:
        assert RLAIF_DESCRIPTION_FRAME == SPEC_RLAIF_DESCRIPTION


class TestComposeRlaifDescription:
    def test_no_purpose_returns_frame_only(self) -> None:
        assert compose_rlaif_description(None) == RLAIF_DESCRIPTION_FRAME
        assert compose_rlaif_description("") == RLAIF_DESCRIPTION_FRAME

    def test_purpose_prepends_frame_intact(self) -> None:
        out = compose_rlaif_description("zap me when i open twitter")
        assert out.endswith(RLAIF_DESCRIPTION_FRAME)
        assert "zap me when i open twitter" in out
        # Header marker so the agent knows the preamble is operator-authored.
        assert out.startswith("Operator purpose:")


# ---------------------------------------------------------------------------
# FastMCP integration — registered tools match the spec.
# ---------------------------------------------------------------------------


def _cfg(*, purpose: str | None = None, **safety: Any) -> Config:
    return Config(
        provider=ProviderConfig(
            kind="pishock",
            raw={"username": "u", "api_key": "k", "sharecode": "s"},
        ),
        device=DeviceConfig(label="test-device"),
        safety=SafetyConfig(**safety),
        tool=ToolConfig(purpose=purpose),
    )


def _device(**kw: Any) -> MockProvider:
    return MockProvider(label="test-device", **kw)


@pytest.mark.asyncio
async def test_registered_tools_match_spec_no_purpose() -> None:
    cfg = _cfg(allow_shock=True)
    state = SafetyState(cfg.safety, now=0.0)
    server = build_server(cfg, state=state, device=_device())
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"rlaif_info", "rlaif_log", "rlaif"}
    assert by_name["rlaif_info"].description == SPEC_RLAIF_INFO_DESCRIPTION
    assert by_name["rlaif_log"].description == SPEC_RLAIF_LOG_DESCRIPTION
    assert by_name["rlaif"].description == SPEC_RLAIF_DESCRIPTION


@pytest.mark.asyncio
async def test_registered_rlaif_tool_uses_purpose_preamble() -> None:
    cfg = _cfg(allow_shock=True, purpose="zap me on focus break")
    state = SafetyState(cfg.safety, now=0.0)
    server = build_server(cfg, state=state, device=_device())
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    desc = by_name["rlaif"].description
    assert desc is not None
    assert desc.endswith(SPEC_RLAIF_DESCRIPTION)
    assert "zap me on focus break" in desc
    # Info / log tool descriptions are NOT affected by purpose.
    assert by_name["rlaif_info"].description == SPEC_RLAIF_INFO_DESCRIPTION
    assert by_name["rlaif_log"].description == SPEC_RLAIF_LOG_DESCRIPTION


# ---------------------------------------------------------------------------
# handle_info
# ---------------------------------------------------------------------------


class TestInfo:
    def test_online_device(self) -> None:
        state = SafetyState(
            SafetyConfig(allow_shock=True, max_intensity=20), now=0.0
        )
        device = _device(api_max_intensity=100, api_max_duration_s=15)
        out = handle_info(state, device)
        assert out["device"]["online"] is True
        assert out["device"]["paused"] is False
        assert out["device"]["api_max_intensity"] == 100
        assert out["config"]["allow_shock"] is True
        assert out["config"]["max_intensity"] == 20
        assert out["rate_limit"]["tokens_available"] == 3

    def test_offline_device_still_returns(self) -> None:
        state = SafetyState(SafetyConfig(), now=0.0)
        device = _device(online=False)
        out = handle_info(state, device)
        assert out["device"]["online"] is False
        # Server state is still observable.
        assert out["config"]["allow_shock"] is False
        assert out["rate_limit"]["tokens_available"] == 3


# ---------------------------------------------------------------------------
# handle_log
# ---------------------------------------------------------------------------


class TestLog:
    def test_empty(self) -> None:
        state = SafetyState(SafetyConfig(allow_shock=True), now=0.0)
        out = handle_log(state, limit=10)
        assert out["entries"] == []
        assert out["retained"] == 0

    def test_reflects_recent_ops(self) -> None:
        state = SafetyState(SafetyConfig(allow_shock=True), now=0.0)
        device = _device()
        logger = MagicMock()
        handle_rlaif(state, device, logger, intensity=1, duration_s=1)
        out = handle_log(state, limit=10)
        assert len(out["entries"]) == 1
        assert out["entries"][0]["device_response"] == "Operation Succeeded."
        assert out["retained"] == 1

    def test_limit_bounds(self) -> None:
        state = SafetyState(SafetyConfig(), now=0.0)
        from mcp.server.fastmcp.exceptions import ToolError
        with pytest.raises(ToolError):
            handle_log(state, limit=0)
        with pytest.raises(ToolError):
            handle_log(state, limit=9999)


# ---------------------------------------------------------------------------
# handle_rlaif — the shock tool
# ---------------------------------------------------------------------------


class TestRlaif:
    def test_allow_shock_false_refuses_without_firing(self) -> None:
        state = SafetyState(SafetyConfig(allow_shock=False), now=0.0)
        device = _device()
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=1, duration_s=1)
        assert out["error"]
        assert "allow_shock" in out["error"]
        assert device.calls == []

    def test_happy_path_fires_and_clamps_in_response(self) -> None:
        state = SafetyState(
            SafetyConfig(allow_shock=True, max_intensity=10, max_duration_s=2),
            now=0.0,
        )
        device = _device()
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=80, duration_s=10)
        assert out["requested"] == {"intensity": 80, "duration_s": 10}
        assert out["actual"] == {"intensity": 10, "duration_s": 2}
        assert out["clamped"] is True
        assert out["device_response"] == "Operation Succeeded."
        # Provider was called with clamped values, not requested values.
        assert device.calls == [(10, 2)]

    def test_rate_limit_refuses_after_capacity(self) -> None:
        state = SafetyState(
            SafetyConfig(allow_shock=True, bucket_capacity=2, refill_seconds=600),
            now=0.0,
        )
        device = _device()
        logger = MagicMock()
        for _ in range(2):
            out = handle_rlaif(state, device, logger, intensity=1, duration_s=1)
            assert out.get("error") is None
            assert out["rate_limited"] is False
        out = handle_rlaif(state, device, logger, intensity=1, duration_s=1)
        assert out["rate_limited"] is True
        assert "next_available_at" in out["error"]
        # Provider was only called twice, not three times.
        assert len(device.calls) == 2

    def test_device_offline_rolls_back_token(self) -> None:
        state = SafetyState(
            SafetyConfig(allow_shock=True, bucket_capacity=1, refill_seconds=600),
            now=0.0,
        )
        device = _device(shock_error=DeviceOfflineError("device offline"))
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=1, duration_s=1)
        assert out["error"] is not None
        assert "device_offline" in out["error"]
        # Token refunded so a subsequent call can succeed.
        assert state.bucket.available(0.0) == 1

    def test_device_paused_rolls_back(self) -> None:
        state = SafetyState(
            SafetyConfig(allow_shock=True, bucket_capacity=1), now=0.0
        )
        device = _device(shock_error=DevicePausedError("paused"))
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=1, duration_s=1)
        assert "device_paused" in out["error"]
        assert state.bucket.available(0.0) == 1

    def test_invalid_input_is_logged_refusal(self) -> None:
        state = SafetyState(SafetyConfig(allow_shock=True), now=0.0)
        device = _device()
        logger = MagicMock()

        out = handle_rlaif(state, device, logger, intensity=0, duration_s=1)
        assert out["error"] is not None
        assert "invalid_input" in out["error"]
        assert "intensity" in out["error"]

        out = handle_rlaif(state, device, logger, intensity=1, duration_s=99)
        assert out["error"] is not None
        assert "invalid_input" in out["error"]
        assert "duration_s" in out["error"]

        # Provider was never called for either.
        assert device.calls == []
        # Both refusals are in the log; no tokens consumed.
        assert len(state.ops_log) == 2
        assert state.bucket.available(0.0) == state.config.bucket_capacity

    def test_high_intensity_flag_propagates(self) -> None:
        state = SafetyState(
            SafetyConfig(
                allow_shock=True, max_intensity=25, warn_threshold_intensity=15
            ),
            now=0.0,
        )
        device = _device()
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=20, duration_s=1)
        assert out["high_intensity"] is True

    def test_warnings_near_ceiling_visible(self) -> None:
        state = SafetyState(
            SafetyConfig(
                allow_shock=True, max_intensity=25, max_duration_s=5
            ),
            now=0.0,
        )
        device = _device()
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=20, duration_s=1)
        assert out.get("warnings") == ["near_ceiling"]


class TestReasonField:
    """The optional `reason` string is audit-only — never gated on, always
    surfaced in the log when supplied."""

    def test_reason_appears_in_response_and_log(self) -> None:
        state = SafetyState(SafetyConfig(allow_shock=True), now=0.0)
        device = _device()
        logger = MagicMock()
        out = handle_rlaif(
            state,
            device,
            logger,
            intensity=1,
            duration_s=1,
            reason="agent saw twitter open",
        )
        assert out.get("reason") == "agent saw twitter open"
        # Reflected in the on-state log too.
        log = handle_log(state, limit=1)
        assert log["entries"][0]["reason"] == "agent saw twitter open"

    def test_missing_reason_omitted(self) -> None:
        state = SafetyState(SafetyConfig(allow_shock=True), now=0.0)
        device = _device()
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=1, duration_s=1)
        assert "reason" not in out

    def test_blank_reason_treated_as_missing(self) -> None:
        state = SafetyState(SafetyConfig(allow_shock=True), now=0.0)
        device = _device()
        logger = MagicMock()
        out = handle_rlaif(
            state, device, logger, intensity=1, duration_s=1, reason="   "
        )
        assert "reason" not in out

    def test_reason_present_on_refusal(self) -> None:
        state = SafetyState(SafetyConfig(allow_shock=False), now=0.0)
        device = _device()
        logger = MagicMock()
        out = handle_rlaif(
            state,
            device,
            logger,
            intensity=1,
            duration_s=1,
            reason="agent thought it was justified",
        )
        assert out["error"]
        # Reason still attached so the operator sees what the agent claimed.
        assert out.get("reason") == "agent thought it was justified"

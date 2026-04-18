"""Server-level tests for rlaif.

Key guarantees locked in here:

* The three tool names and their description strings match the build spec
  byte-for-byte. If someone edits a description, a test fails.
* The safety layer's behavior is visible through the tool surface
  (allow_shock=false refuses, rate limit trips at capacity, clamping shows
  up as requested vs actual, device_offline comes back as an error record).
* ``pishock`` is never called — every test uses a fake device.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pishock
import pytest

from rlaif.config import AuthConfig, Config, DeviceConfig
from rlaif.safety import SafetyConfig, SafetyState
from rlaif.server import (
    RLAIF_DESCRIPTION,
    RLAIF_INFO_DESCRIPTION,
    RLAIF_LOG_DESCRIPTION,
    Device,
    build_server,
    handle_info,
    handle_log,
    handle_rlaif,
)


# ---------------------------------------------------------------------------
# Spec-mandated description strings. These are the verbatim strings from the
# build prompt. If the description in server.py drifts from the spec, these
# comparisons fail.
# ---------------------------------------------------------------------------

SPEC_RLAIF_INFO_DESCRIPTION = (
    "Report PiShock device status and current rlaif server state. Does not "
    "touch the device hardware. Returns: {device: {name, online, paused, "
    "api_max_intensity, api_max_duration_s}, config: {allow_shock, "
    "max_intensity, max_duration_s, bucket_capacity, refill_seconds}, "
    "rate_limit: {tokens_available, next_refill_at}}."
)

SPEC_RLAIF_LOG_DESCRIPTION = (
    "Return up to `limit` most recent rlaif operations from the in-memory "
    "log (default limit 10, max 200 entries retained, cleared on server "
    "restart). Each entry: {op_id, timestamp, requested: {intensity, "
    "duration_s}, actual: {intensity, duration_s}, clamped, rate_limited, "
    "high_intensity, device_response, error?}. Useful for reviewing what "
    "has been fired in this session."
)

SPEC_RLAIF_DESCRIPTION = (
    "Fire a shock on the user's PiShock device. This is a real physical "
    "action on hardware the user has explicitly set up.\n"
    "\n"
    "Parameters:\n"
    "- intensity: 1–100. The server clamps this to the configured cap "
    "(default 25, hard ceiling 50). Requested values above the cap are "
    "silently reduced; the response distinguishes `requested` from "
    "`actual`.\n"
    "- duration_s: 1–15 seconds. Clamped to the configured duration cap "
    "(default 2s, hard ceiling 5s).\n"
    "\n"
    "Rate limiting: token bucket (default capacity 3, refill 1 token per "
    "600s). Calls exceeding the bucket are refused outright (do not fire); "
    "the response contains `rate_limited: true` and `next_available_at`.\n"
    "\n"
    "Hard refusal: if `allow_shock: false` in server config, all calls are "
    "refused with an actionable error message.\n"
    "\n"
    "Returns: {op_id, timestamp, requested: {intensity, duration_s}, "
    "actual: {intensity, duration_s}, clamped: bool, rate_limited: bool, "
    "high_intensity: bool, device_response: str, error?: str}."
)


class TestDescriptionsMatchSpec:
    """If someone edits a description in server.py, these tests fail."""

    def test_rlaif_info_description(self) -> None:
        assert RLAIF_INFO_DESCRIPTION == SPEC_RLAIF_INFO_DESCRIPTION

    def test_rlaif_log_description(self) -> None:
        assert RLAIF_LOG_DESCRIPTION == SPEC_RLAIF_LOG_DESCRIPTION

    def test_rlaif_description(self) -> None:
        assert RLAIF_DESCRIPTION == SPEC_RLAIF_DESCRIPTION


# ---------------------------------------------------------------------------
# FastMCP integration — registered tools match the spec.
# ---------------------------------------------------------------------------


def _cfg(**safety: Any) -> Config:
    return Config(
        auth=AuthConfig(username="u", api_key="k", sharecode="s"),
        device=DeviceConfig(label="test-device"),
        safety=SafetyConfig(**safety),
    )


def _fake_device(online: bool = True, paused: bool = False) -> Device:
    """Build a Device wrapping a mocked pishock shocker."""
    shocker = MagicMock(spec=pishock.HTTPShocker)
    info = MagicMock()
    info.name = "test-device"
    info.is_paused = paused
    info.max_intensity = 100
    info.max_duration = 15
    if online:
        shocker.info.return_value = info
    else:
        shocker.info.side_effect = pishock.DeviceNotConnectedError("offline")
    shocker.shock.return_value = None
    api = MagicMock(spec=pishock.PiShockAPI)
    return Device(api=api, shocker=shocker, label="test-device")


@pytest.mark.asyncio
async def test_registered_tools_match_spec() -> None:
    cfg = _cfg(allow_shock=True)
    state = SafetyState(cfg.safety, now=0.0)
    device = _fake_device()
    server = build_server(cfg, state=state, device=device)
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"rlaif_info", "rlaif_log", "rlaif"}
    assert by_name["rlaif_info"].description == SPEC_RLAIF_INFO_DESCRIPTION
    assert by_name["rlaif_log"].description == SPEC_RLAIF_LOG_DESCRIPTION
    assert by_name["rlaif"].description == SPEC_RLAIF_DESCRIPTION


# ---------------------------------------------------------------------------
# handle_info
# ---------------------------------------------------------------------------


class TestInfo:
    def test_online_device(self) -> None:
        state = SafetyState(
            SafetyConfig(allow_shock=True, max_intensity=20), now=0.0
        )
        device = _fake_device(online=True, paused=False)
        out = handle_info(state, device)
        assert out["device"]["online"] is True
        assert out["device"]["paused"] is False
        assert out["device"]["api_max_intensity"] == 100
        assert out["config"]["allow_shock"] is True
        assert out["config"]["max_intensity"] == 20
        assert out["rate_limit"]["tokens_available"] == 3

    def test_offline_device_still_returns(self) -> None:
        state = SafetyState(SafetyConfig(), now=0.0)
        device = _fake_device(online=False)
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
        device = _fake_device()
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
        device = _fake_device()
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=1, duration_s=1)
        assert out["error"]  # error string present
        assert "allow_shock" in out["error"]
        device.shocker.shock.assert_not_called()

    def test_happy_path_fires_and_clamps_in_response(self) -> None:
        state = SafetyState(
            SafetyConfig(allow_shock=True, max_intensity=10, max_duration_s=2),
            now=0.0,
        )
        device = _fake_device()
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=80, duration_s=10)
        assert out["requested"] == {"intensity": 80, "duration_s": 10}
        assert out["actual"] == {"intensity": 10, "duration_s": 2}
        assert out["clamped"] is True
        assert out["device_response"] == "Operation Succeeded."
        # Device was called with clamped values, not requested values.
        device.shocker.shock.assert_called_once()
        kwargs = device.shocker.shock.call_args.kwargs
        assert kwargs["intensity"] == 10
        assert kwargs["duration"] == 2

    def test_rate_limit_refuses_after_capacity(self) -> None:
        state = SafetyState(
            SafetyConfig(allow_shock=True, bucket_capacity=2, refill_seconds=600),
            now=0.0,
        )
        device = _fake_device()
        logger = MagicMock()
        for _ in range(2):
            out = handle_rlaif(state, device, logger, intensity=1, duration_s=1)
            assert out.get("error") is None
            assert out["rate_limited"] is False
        out = handle_rlaif(state, device, logger, intensity=1, duration_s=1)
        assert out["rate_limited"] is True
        assert "next_available_at" in out["error"]
        # Device was only called twice, not three times.
        assert device.shocker.shock.call_count == 2

    def test_device_offline_rolls_back_token(self) -> None:
        state = SafetyState(
            SafetyConfig(allow_shock=True, bucket_capacity=1, refill_seconds=600),
            now=0.0,
        )
        shocker = MagicMock(spec=pishock.HTTPShocker)
        shocker.shock.side_effect = pishock.DeviceNotConnectedError("device offline")
        api = MagicMock(spec=pishock.PiShockAPI)
        device = Device(api=api, shocker=shocker, label="test")
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=1, duration_s=1)
        assert out["error"] is not None
        assert "device_offline" in out["error"]
        # Token refunded so a subsequent call (once device comes back) can succeed.
        assert state.bucket.available(0.0) == 1

    def test_device_paused_rolls_back(self) -> None:
        state = SafetyState(
            SafetyConfig(allow_shock=True, bucket_capacity=1), now=0.0
        )
        shocker = MagicMock(spec=pishock.HTTPShocker)
        shocker.shock.side_effect = pishock.ShockerPausedError("paused")
        api = MagicMock(spec=pishock.PiShockAPI)
        device = Device(api=api, shocker=shocker, label="test")
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=1, duration_s=1)
        assert "device_paused" in out["error"]
        assert state.bucket.available(0.0) == 1

    def test_invalid_input_raises_tool_error(self) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        state = SafetyState(SafetyConfig(allow_shock=True), now=0.0)
        device = _fake_device()
        logger = MagicMock()
        with pytest.raises(ToolError):
            handle_rlaif(state, device, logger, intensity=0, duration_s=1)
        with pytest.raises(ToolError):
            handle_rlaif(state, device, logger, intensity=1, duration_s=99)

    def test_high_intensity_flag_propagates(self) -> None:
        state = SafetyState(
            SafetyConfig(
                allow_shock=True, max_intensity=25, warn_threshold_intensity=15
            ),
            now=0.0,
        )
        device = _fake_device()
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
        device = _fake_device()
        logger = MagicMock()
        out = handle_rlaif(state, device, logger, intensity=20, duration_s=1)
        assert out.get("warnings") == ["near_ceiling"]

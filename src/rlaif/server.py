"""MCP wiring for rlaif. Thin by design — the heavy lifting lives in
:mod:`rlaif.safety`, the negative-channel device code lives in
:mod:`rlaif.providers`, and the positive-channel device code lives in
:mod:`rlaif.rewards`.

Up to four tools are registered:

* ``rlaif_info`` — read-only device + server state for both channels.
* ``rlaif_log`` — recent ops from both channels' logs, interleaved.
* ``negative`` — fire a negative stimulus (gated by
  the negative channel's safety layer). Registered only when
  ``[negative]`` is configured.
* ``positive`` — fire a positive stimulus (gated by
  the positive channel's safety layer). Registered only when
  ``[positive]`` is configured.

Tool descriptions are split into two parts:

* a verbatim spec ``*_FRAME`` constant — locked by ``test_server.py``.
* an optional operator-authored ``purpose`` preamble — pulled from the
  matching channel's ``[<channel>.tool] purpose = "..."`` and prepended
  to that channel's fire-tool description.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from rlaif.config import (
    ChannelConfig,
    Config,
    ConfigError,
    default_config_path,
    default_log_path,
    load,
)
from rlaif.providers import (
    DeviceOfflineError,
    DevicePausedError,
    Provider,
    ProviderAuthError,
    ProviderError,
    ShockNotAllowedError,
    build_provider,
)
from rlaif.rewards import (
    RewardDeviceOfflineError,
    RewardProvider,
    RewardProviderAuthError,
    RewardProviderError,
    RewardWatchdogError,
    build_reward_provider,
)
from rlaif.safety import (
    OPS_LOG_CAPACITY,
    OPS_LOG_DEFAULT_LIMIT,
    OpRecord,
    SafetyState,
)

# ---------------------------------------------------------------------------
# Tool description frames. ``test_server.py`` asserts these match verbatim.
# Provider-agnostic wording — the safety envelope is the same regardless of
# which backend dispatches the call.
# ---------------------------------------------------------------------------

RLAIF_INFO_DESCRIPTION_FRAME = (
    "Report rlaif device + server state across both channels. Does not "
    "trigger any device.\n"
    "Returns: {negative?: {channel, device: {name, online, paused, "
    "api_max_intensity, api_max_duration_s}, config: {allow, max_intensity, "
    "max_duration_s, bucket_capacity, refill_seconds}, rate_limit: "
    "{tokens_available, next_refill_at}}, positive?: {channel, device: "
    "{name, online, actuators}, config: {...}, rate_limit: {...}}}. "
    "A channel block is omitted when that channel is not configured."
)

RLAIF_LOG_DESCRIPTION_FRAME = (
    "Return up to `limit` most recent rlaif operations from both channels' "
    "logs, newest first (default limit 10, max 200 entries retained per "
    "channel).\n"
    "Returns: {entries: [{op_id, timestamp, channel, requested: {intensity, "
    "duration_s}, actual: {intensity, duration_s}, clamped, rate_limited, "
    "high_intensity, device_response, reason?, error?}], retained: int}."
)

RLAIF_NEGATIVE_DESCRIPTION_FRAME = (
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

RLAIF_POSITIVE_DESCRIPTION_FRAME = (
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


def _compose_description(frame: str, purpose: str | None) -> str:
    """Prepend an operator-authored purpose preamble to a description frame."""
    if not purpose:
        return frame
    return f"Operator purpose:\n{purpose}\n\n{frame}"


# ---------------------------------------------------------------------------
# logging setup — structlog JSON to stderr.
# ---------------------------------------------------------------------------


def configure_logging() -> structlog.stdlib.BoundLogger:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger("rlaif")


# ---------------------------------------------------------------------------
# Channel runtime state — one per active channel.
# ---------------------------------------------------------------------------


class NegativeRuntime:
    """Bundle of safety state + provider for the negative channel."""

    def __init__(self, state: SafetyState, device: Provider) -> None:
        self.state = state
        self.device = device


class PositiveRuntime:
    """Bundle of safety state + provider for the positive channel."""

    def __init__(self, state: SafetyState, device: RewardProvider) -> None:
        self.state = state
        self.device = device


# ---------------------------------------------------------------------------
# Tool handlers. Module-level + pure so tests can call them with a fake
# provider.
# ---------------------------------------------------------------------------


def _negative_info(rt: NegativeRuntime) -> dict[str, Any]:
    info = rt.device.info()
    block: dict[str, Any] = {
        "name": info.name,
        "online": info.online,
        "paused": info.paused,
        "api_max_intensity": info.api_max_intensity,
        "api_max_duration_s": info.api_max_duration_s,
    }
    if info.error is not None:
        block["error"] = info.error
    return rt.state.info_snapshot(device=block)


def _positive_info(rt: PositiveRuntime) -> dict[str, Any]:
    info = rt.device.info()
    block: dict[str, Any] = {
        "name": info.name,
        "online": info.online,
        "actuators": info.actuators,
    }
    if info.paused is not None:
        block["paused"] = info.paused
    if info.error is not None:
        block["error"] = info.error
    return rt.state.info_snapshot(device=block)


def handle_info(
    *,
    negative: NegativeRuntime | None,
    positive: PositiveRuntime | None,
) -> dict[str, Any]:
    """Combined ``rlaif_info`` payload across both channels.

    A channel block is omitted entirely when that channel is not
    configured. The agent should treat absence as "this channel is not
    available", not as an error condition.
    """
    out: dict[str, Any] = {}
    if negative is not None:
        out["negative"] = _negative_info(negative)
    if positive is not None:
        out["positive"] = _positive_info(positive)
    return out


def handle_log(
    *,
    negative: NegativeRuntime | None,
    positive: PositiveRuntime | None,
    limit: int = OPS_LOG_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Combined ``rlaif_log`` payload — interleaves both channels' ops by timestamp."""
    if not 1 <= limit <= OPS_LOG_CAPACITY:
        raise ToolError(f"limit must be between 1 and {OPS_LOG_CAPACITY}, got {limit}")
    entries: list[OpRecord] = []
    retained = 0
    if negative is not None:
        entries.extend(negative.state.ops_log.recent(min(limit, OPS_LOG_CAPACITY)))
        retained += len(negative.state.ops_log)
    if positive is not None:
        entries.extend(positive.state.ops_log.recent(min(limit, OPS_LOG_CAPACITY)))
        retained += len(positive.state.ops_log)
    # Interleave by timestamp, newest first.
    entries.sort(key=lambda r: r.timestamp, reverse=True)
    return {
        "entries": [r.to_dict() for r in entries[:limit]],
        "retained": retained,
    }


def _fire_channel(
    *,
    state: SafetyState,
    logger: structlog.stdlib.BoundLogger,
    intensity: int,
    duration_s: int,
    reason: str | None,
    log_prefix: str,
    device_fn: Callable[[int, int], str],
    dispatch: Mapping[type[BaseException], tuple[str, bool]],
) -> dict[str, Any]:
    """Run the authorize → fire → commit|rollback loop for one channel.

    ``dispatch`` maps a typed exception class to ``(log_key_suffix, refund)``.
    Entries are matched in insertion order via ``isinstance``, so put more
    specific subclasses ahead of their bases. ``refund=False`` doubles as the
    "this is bad enough to log at error level" signal — currently the only
    such case is the positive-channel watchdog (see CLAUDE.md hard rule #6).

    The helper is type-erased over the channel: each caller is responsible
    for handing in a dispatch table populated only with its own channel's
    error types, which keeps ``Provider`` and ``RewardProvider`` namespaces
    disjoint at the call site (CLAUDE.md hard rule #8). Anything not in
    ``dispatch`` falls through to the ``unexpected`` handler.
    """
    rec = state.authorize(intensity=intensity, duration_s=duration_s, reason=reason)
    if rec.error is not None or rec.rate_limited:
        logger.info(
            f"{log_prefix}.refused",
            op_id=rec.op_id,
            rate_limited=rec.rate_limited,
            error=rec.error,
            reason=rec.reason,
        )
        return rec.to_dict()

    logger.info(
        f"{log_prefix}.authorized",
        op_id=rec.op_id,
        actual=rec.actual,
        requested=rec.requested,
        clamped=rec.clamped,
        high_intensity=rec.high_intensity,
        warnings=rec.warnings,
        reason=rec.reason,
    )
    try:
        resp = device_fn(rec.actual["intensity"], rec.actual["duration_s"])
    except Exception as exc:
        for exc_type, (suffix, refund) in dispatch.items():
            if isinstance(exc, exc_type):
                final = state.rollback(rec, error=f"{suffix}: {exc}", refund=refund)
                log = logger.warning if refund else logger.error
                log(f"{log_prefix}.{suffix}", op_id=rec.op_id, error=str(exc))
                return final.to_dict()
        final = state.rollback(  # pragma: no cover - defensive
            rec, error=f"unexpected: {type(exc).__name__}: {exc}"
        )
        logger.error(  # pragma: no cover - defensive
            f"{log_prefix}.unexpected", op_id=rec.op_id, error=str(exc)
        )
        return final.to_dict()  # pragma: no cover - defensive

    final = state.commit(rec, device_response=resp)
    logger.info(f"{log_prefix}.fired", op_id=rec.op_id, device_response=resp)
    return final.to_dict()


_NEGATIVE_DISPATCH: Mapping[type[BaseException], tuple[str, bool]] = {
    DeviceOfflineError: ("device_offline", True),
    DevicePausedError: ("device_paused", True),
    ShockNotAllowedError: ("shock_not_allowed", True),
    ProviderAuthError: ("auth_error", True),
    ProviderError: ("provider_error", True),
}

_POSITIVE_DISPATCH: Mapping[type[BaseException], tuple[str, bool]] = {
    RewardDeviceOfflineError: ("device_offline", True),
    # Watchdog: device may still be running. Do NOT refund the token —
    # see CLAUDE.md hard rule #6 and RewardWatchdogError's docstring.
    RewardWatchdogError: ("watchdog", False),
    RewardProviderAuthError: ("auth_error", True),
    RewardProviderError: ("provider_error", True),
}


def handle_rlaif_negative(
    rt: NegativeRuntime,
    logger: structlog.stdlib.BoundLogger,
    intensity: int,
    duration_s: int,
    reason: str | None = None,
) -> dict[str, Any]:
    def device_fn(intensity: int, duration_s: int) -> str:
        return rt.device.shock(intensity=intensity, duration_s=duration_s)

    return _fire_channel(
        state=rt.state,
        logger=logger,
        intensity=intensity,
        duration_s=duration_s,
        reason=reason,
        log_prefix="rlaif.negative",
        device_fn=device_fn,
        dispatch=_NEGATIVE_DISPATCH,
    )


def handle_rlaif_positive(
    rt: PositiveRuntime,
    logger: structlog.stdlib.BoundLogger,
    intensity: int,
    duration_s: int,
    reason: str | None = None,
) -> dict[str, Any]:
    def device_fn(intensity: int, duration_s: int) -> str:
        return rt.device.vibrate(intensity=intensity, duration_s=duration_s)

    return _fire_channel(
        state=rt.state,
        logger=logger,
        intensity=intensity,
        duration_s=duration_s,
        reason=reason,
        log_prefix="rlaif.positive",
        device_fn=device_fn,
        dispatch=_POSITIVE_DISPATCH,
    )


# ---------------------------------------------------------------------------
# Server assembly.
# ---------------------------------------------------------------------------


def build_file_sink(path: Path, logger: structlog.stdlib.BoundLogger) -> Callable[[OpRecord], None]:
    """Return a sink that appends one JSON line per op to ``path``.

    Each write flushes and ``fsync``s the file before close — the safety
    story (every fired op survives a crash) is only honest if the bytes
    have actually hit the disk. Latency is invisible on SSD.
    """
    created = False

    def sink(record: OpRecord) -> None:
        nonlocal created
        try:
            if not created:
                path.parent.mkdir(parents=True, exist_ok=True)
                created = True
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(), default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            logger.warning("rlaif.log_sink_failed", path=str(path), error=str(exc))

    return sink


def build_negative_runtime(
    cc: ChannelConfig,
    *,
    state: SafetyState | None = None,
    device: Provider | None = None,
    on_record: Callable[[OpRecord], None] | None = None,
) -> NegativeRuntime:
    if state is None:
        state = SafetyState(cc.safety, on_record=on_record)
    if device is None:
        device = build_provider(cc.kind, cc.raw, label=cc.label)
    return NegativeRuntime(state=state, device=device)


def build_positive_runtime(
    cc: ChannelConfig,
    *,
    state: SafetyState | None = None,
    device: RewardProvider | None = None,
    on_record: Callable[[OpRecord], None] | None = None,
) -> PositiveRuntime:
    if state is None:
        state = SafetyState(cc.safety, on_record=on_record)
    if device is None:
        device = build_reward_provider(cc.kind, cc.raw, label=cc.label)
    return PositiveRuntime(state=state, device=device)


def build_server(
    cfg: Config,
    *,
    negative: NegativeRuntime | None = None,
    positive: PositiveRuntime | None = None,
    logger: structlog.stdlib.BoundLogger | None = None,
) -> FastMCP:
    """Construct the FastMCP server wired to the provided channel runtimes.

    Each channel is independently optional. If ``cfg.negative`` is None,
    no negative-side runtime is constructed and ``negative`` is not
    registered. Same for positive. Tests inject pre-built runtimes; the
    main entry point in :func:`main` builds them from config.
    """
    bound_logger: structlog.stdlib.BoundLogger = logger if logger is not None else structlog.get_logger("rlaif")

    if negative is None and cfg.negative is not None:
        negative = build_negative_runtime(cfg.negative)
    if positive is None and cfg.positive is not None:
        positive = build_positive_runtime(cfg.positive)

    if negative is None and positive is None:
        # The config layer should have caught this already, but keep a
        # defensive check so we never silently start with zero tools.
        raise ConfigError("at least one of [negative] / [positive] must be configured")

    mcp = FastMCP(
        name="rlaif",
        instructions=(
            "MCP server for user-owned reinforcement devices. "
            "`rlaif_info` and `rlaif_log` are always safe and cover both channels. "
            "`negative` fires negative reinforcement (a real shock). "
            "`positive` fires positive reinforcement (a real vibration). "
            "Either fire-tool may be absent depending on which channels the "
            "operator configured."
        ),
    )

    @mcp.tool(name="rlaif_info", description=RLAIF_INFO_DESCRIPTION_FRAME)
    def rlaif_info() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return handle_info(negative=negative, positive=positive)

    @mcp.tool(name="rlaif_log", description=RLAIF_LOG_DESCRIPTION_FRAME)
    def rlaif_log(  # pyright: ignore[reportUnusedFunction]
        limit: int = OPS_LOG_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        return handle_log(negative=negative, positive=positive, limit=limit)

    if negative is not None:
        neg_purpose = cfg.negative.purpose if cfg.negative is not None else None
        neg_description = _compose_description(RLAIF_NEGATIVE_DESCRIPTION_FRAME, neg_purpose)
        neg_rt = negative

        @mcp.tool(name="negative", description=neg_description)
        def _negative_tool(  # pyright: ignore[reportUnusedFunction]
            intensity: int, duration_s: int, reason: str | None = None
        ) -> dict[str, Any]:
            return handle_rlaif_negative(neg_rt, bound_logger, intensity, duration_s, reason)

    if positive is not None:
        pos_purpose = cfg.positive.purpose if cfg.positive is not None else None
        pos_description = _compose_description(RLAIF_POSITIVE_DESCRIPTION_FRAME, pos_purpose)
        pos_rt = positive

        @mcp.tool(name="positive", description=pos_description)
        def _positive_tool(  # pyright: ignore[reportUnusedFunction]
            intensity: int, duration_s: int, reason: str | None = None
        ) -> dict[str, Any]:
            return handle_rlaif_positive(pos_rt, bound_logger, intensity, duration_s, reason)

    return mcp


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    logger = configure_logging()
    try:
        cfg_path = default_config_path()
        cfg = load(cfg_path)
    except ConfigError as exc:
        logger.error("rlaif.config_error", error=str(exc))
        print(f"rlaif: config error: {exc}", file=sys.stderr)
        return 2

    log_path = default_log_path()
    sink = build_file_sink(log_path, logger)
    logger.info(
        "rlaif.starting",
        config_path=str(default_config_path()),
        log_path=str(log_path),
        config=cfg.redacted(),
    )

    negative: NegativeRuntime | None = None
    positive: PositiveRuntime | None = None
    if cfg.negative is not None:
        try:
            negative = build_negative_runtime(cfg.negative, on_record=sink)
        except Exception as exc:  # pragma: no cover - startup wiring
            logger.error("rlaif.negative.startup_error", error=str(exc))
            return 3
        info = negative.device.info()
        logger.info(
            "rlaif.negative.startup_probe",
            provider=cfg.negative.kind,
            device={
                "name": info.name,
                "online": info.online,
                "paused": info.paused,
                "error": info.error,
            },
        )

    if cfg.positive is not None:
        try:
            positive = build_positive_runtime(cfg.positive, on_record=sink)
        except Exception as exc:  # pragma: no cover - startup wiring
            logger.error("rlaif.positive.startup_error", error=str(exc))
            return 3
        info = positive.device.info()
        logger.info(
            "rlaif.positive.startup_probe",
            provider=cfg.positive.kind,
            device={
                "name": info.name,
                "online": info.online,
                "actuators": info.actuators,
                "error": info.error,
            },
        )

    server = build_server(cfg, negative=negative, positive=positive, logger=logger)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

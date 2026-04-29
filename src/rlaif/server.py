"""MCP wiring for rlaif. Thin by design — the heavy lifting lives in
:mod:`rlaif.safety`, and the device-touching code lives in
:mod:`rlaif.providers`.

Three tools are registered:

* ``rlaif_info`` — read-only device + server state.
* ``rlaif_log`` — recent ops from the in-memory log.
* ``rlaif`` — fire a shock (gated by every check in the safety layer).

Tool descriptions are split into two parts:

* a verbatim spec ``*_FRAME`` constant — locked by ``test_server.py``.
* an optional operator-authored ``purpose`` preamble — pulled from
  ``[tool] purpose = "..."`` in the config and prepended to the ``rlaif``
  tool description. Lets the user tell the agent *when* to shock without
  monkey-patching the spec.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from rlaif.config import (
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
from rlaif.providers.pishock import PiShockProvider
from rlaif.safety import (
    OPS_LOG_CAPACITY,
    OPS_LOG_DEFAULT_LIMIT,
    OpRecord,
    SafetyState,
)

# ---------------------------------------------------------------------------
# Tool description frames. ``test_server.py`` asserts these match verbatim.
# Provider-agnostic wording — the safety envelope is the same regardless of
# which backend dispatches the shock.
# ---------------------------------------------------------------------------

RLAIF_INFO_DESCRIPTION_FRAME = (
    "Report current shock device status and rlaif server state. Does not "
    "trigger the device.\n"
    "Returns: {device: {name, online, paused, "
    "api_max_intensity, api_max_duration_s}, config: {allow_shock, "
    "max_intensity, max_duration_s, bucket_capacity, refill_seconds}, "
    "rate_limit: {tokens_available, next_refill_at}}."
)

RLAIF_LOG_DESCRIPTION_FRAME = (
    "Return up to `limit` most recent rlaif operations from the log "
    "(default limit 10, max 200 entries retained).\n"
    "Returns: {op_id, timestamp, requested: {intensity, "
    "duration_s}, actual: {intensity, duration_s}, clamped, rate_limited, "
    "high_intensity, device_response, reason?, error?}."
)

RLAIF_DESCRIPTION_FRAME = (
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


def compose_rlaif_description(purpose: str | None) -> str:
    """Build the ``rlaif`` tool description.

    The spec frame is always present and tail-aligned so the parameter
    docs read the same regardless of whether a purpose is configured. An
    operator-authored purpose preamble (from ``[tool] purpose = ...``) is
    prepended verbatim and separated by a blank line.
    """
    if not purpose:
        return RLAIF_DESCRIPTION_FRAME
    return f"Operator purpose:\n{purpose}\n\n{RLAIF_DESCRIPTION_FRAME}"


# Back-compat exports — older callers import these names. The frame strings
# are the source of truth; the unprefixed names alias them for now.
RLAIF_INFO_DESCRIPTION = RLAIF_INFO_DESCRIPTION_FRAME
RLAIF_LOG_DESCRIPTION = RLAIF_LOG_DESCRIPTION_FRAME
RLAIF_DESCRIPTION = RLAIF_DESCRIPTION_FRAME


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
# Device — a back-compat alias around :class:`PiShockProvider`.
# ---------------------------------------------------------------------------


class Device(PiShockProvider):
    """Back-compat constructor for tests that build a PiShock provider from
    pre-built ``api`` and ``shocker`` mocks.

    New code should use a :class:`Provider` directly.
    """

    def __init__(self, *, api: Any, shocker: Any, label: str) -> None:
        # We bypass the upstream pishock client construction by setting the
        # internal attributes directly. The base class ``__init__`` would
        # otherwise reach the network on construction.
        self.label = label
        self._api = api
        self._shocker = shocker


# ---------------------------------------------------------------------------
# Tool handlers. Kept module-level + pure so tests can call them with a
# fake provider.
# ---------------------------------------------------------------------------


def handle_info(state: SafetyState, device: Provider) -> dict[str, Any]:
    info = device.info()
    block: dict[str, Any] = {
        "name": info.name,
        "online": info.online,
        "paused": info.paused,
        "api_max_intensity": info.api_max_intensity,
        "api_max_duration_s": info.api_max_duration_s,
    }
    if info.error is not None:
        block["error"] = info.error
    return state.info_snapshot(device=block)


def handle_log(state: SafetyState, limit: int = OPS_LOG_DEFAULT_LIMIT) -> dict[str, Any]:
    if not 1 <= limit <= OPS_LOG_CAPACITY:
        raise ToolError(
            f"limit must be between 1 and {OPS_LOG_CAPACITY}, got {limit}"
        )
    return {"entries": state.log_snapshot(limit=limit), "retained": len(state.ops_log)}


def handle_rlaif(
    state: SafetyState,
    device: Provider,
    logger: structlog.stdlib.BoundLogger,
    intensity: int,
    duration_s: int,
    reason: str | None = None,
) -> dict[str, Any]:
    rec = state.authorize(intensity=intensity, duration_s=duration_s, reason=reason)
    if rec.error is not None or rec.rate_limited:
        logger.info(
            "rlaif.refused",
            op_id=rec.op_id,
            rate_limited=rec.rate_limited,
            error=rec.error,
            reason=rec.reason,
        )
        return rec.to_dict()

    logger.info(
        "rlaif.authorized",
        op_id=rec.op_id,
        actual=rec.actual,
        requested=rec.requested,
        clamped=rec.clamped,
        high_intensity=rec.high_intensity,
        warnings=rec.warnings,
        reason=rec.reason,
    )
    try:
        resp = device.shock(
            intensity=rec.actual["intensity"], duration_s=rec.actual["duration_s"]
        )
    except DeviceOfflineError as exc:
        final = state.rollback(rec, error=f"device_offline: {exc}")
        logger.warning("rlaif.device_offline", op_id=rec.op_id, error=str(exc))
        return final.to_dict()
    except DevicePausedError as exc:
        final = state.rollback(rec, error=f"device_paused: {exc}")
        logger.warning("rlaif.device_paused", op_id=rec.op_id, error=str(exc))
        return final.to_dict()
    except ShockNotAllowedError as exc:
        final = state.rollback(rec, error=f"shock_not_allowed: {exc}")
        logger.warning("rlaif.shock_not_allowed", op_id=rec.op_id, error=str(exc))
        return final.to_dict()
    except ProviderAuthError as exc:
        final = state.rollback(rec, error=f"auth_error: {exc}")
        logger.warning("rlaif.auth_error", op_id=rec.op_id, error=str(exc))
        return final.to_dict()
    except ProviderError as exc:
        final = state.rollback(rec, error=f"{type(exc).__name__}: {exc}")
        logger.warning("rlaif.provider_error", op_id=rec.op_id, error=str(exc))
        return final.to_dict()
    except Exception as exc:  # pragma: no cover - defensive
        final = state.rollback(rec, error=f"unexpected: {type(exc).__name__}: {exc}")
        logger.error("rlaif.unexpected", op_id=rec.op_id, error=str(exc))
        return final.to_dict()

    final = state.commit(rec, device_response=resp)
    logger.info("rlaif.fired", op_id=rec.op_id, device_response=resp)
    return final.to_dict()


# ---------------------------------------------------------------------------
# Server assembly.
# ---------------------------------------------------------------------------


def build_file_sink(
    path: Path, logger: structlog.stdlib.BoundLogger
) -> Callable[[OpRecord], None]:
    """Return a sink that appends one JSON line per op to ``path``."""
    created = False

    def sink(record: OpRecord) -> None:
        nonlocal created
        try:
            if not created:
                path.parent.mkdir(parents=True, exist_ok=True)
                created = True
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(), default=str) + "\n")
        except OSError as exc:
            logger.warning(
                "rlaif.log_sink_failed", path=str(path), error=str(exc)
            )

    return sink


def build_server(
    cfg: Config,
    *,
    state: SafetyState | None = None,
    device: Provider | None = None,
    logger: structlog.stdlib.BoundLogger | None = None,
) -> FastMCP:
    """Construct the FastMCP server wired to the provided safety/provider objects.

    All collaborators can be injected for tests.
    """
    bound_logger: structlog.stdlib.BoundLogger = (
        logger if logger is not None else structlog.get_logger("rlaif")
    )
    bound_state: SafetyState = state if state is not None else SafetyState(cfg.safety)
    if device is None:
        device = build_provider(cfg.provider.kind, cfg.provider.raw, label=cfg.device.label)
    bound_device: Provider = device

    rlaif_description = compose_rlaif_description(cfg.tool.purpose)

    mcp = FastMCP(
        name="rlaif",
        instructions=(
            "MCP server for a user-owned shock collar. `rlaif_info` is always safe."
            "`rlaif_log` shows recent ops. `rlaif` fires a real shock."
        ),
    )

    @mcp.tool(name="rlaif_info", description=RLAIF_INFO_DESCRIPTION_FRAME)
    def rlaif_info() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return handle_info(bound_state, bound_device)

    @mcp.tool(name="rlaif_log", description=RLAIF_LOG_DESCRIPTION_FRAME)
    def rlaif_log(  # pyright: ignore[reportUnusedFunction]
        limit: int = OPS_LOG_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        return handle_log(bound_state, limit=limit)

    @mcp.tool(name="rlaif", description=rlaif_description)
    def rlaif(  # pyright: ignore[reportUnusedFunction]
        intensity: int, duration_s: int, reason: str | None = None
    ) -> dict[str, Any]:
        return handle_rlaif(
            bound_state, bound_device, bound_logger, intensity, duration_s, reason
        )

    return mcp


def main(argv: list[str] | None = None) -> int:
    logger = configure_logging()
    try:
        cfg_path = default_config_path()
        cfg = load(cfg_path)
    except ConfigError as exc:
        logger.error("rlaif.config_error", error=str(exc))
        print(f"rlaif: config error: {exc}", file=sys.stderr)
        return 2

    log_path = default_log_path()
    logger.info(
        "rlaif.starting",
        config_path=str(default_config_path()),
        log_path=str(log_path),
        config=cfg.redacted(),
    )

    state = SafetyState(cfg.safety, on_record=build_file_sink(log_path, logger))
    try:
        device = build_provider(cfg.provider.kind, cfg.provider.raw, label=cfg.device.label)
    except Exception as exc:  # pragma: no cover - startup wiring
        logger.error("rlaif.startup_error", error=str(exc))
        return 3

    # Startup probe — best-effort. If the device is offline we still run so
    # `rlaif_info` and `rlaif_log` stay usable.
    info = device.info()
    logger.info(
        "rlaif.startup_probe",
        provider=cfg.provider.kind,
        device={
            "name": info.name,
            "online": info.online,
            "paused": info.paused,
            "error": info.error,
        },
    )

    server = build_server(cfg, state=state, device=device, logger=logger)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

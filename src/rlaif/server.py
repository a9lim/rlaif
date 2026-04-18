"""MCP wiring for rlaif. Thin by design — the heavy lifting lives in :mod:`rlaif.safety`.

Three tools are registered:

* ``rlaif_info`` — read-only device + server state.
* ``rlaif_log`` — recent ops from the in-memory log.
* ``rlaif`` — fire a shock (gated by every check in the safety layer).

Tool ``description`` strings are module-level constants so :mod:`tests.test_server`
can assert the registered surface matches the spec byte-for-byte.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pishock
import structlog
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from rlaif.config import Config, ConfigError, default_config_path, load
from rlaif.safety import (
    DURATION_INPUT_MAX_S,
    DURATION_INPUT_MIN_S,
    INTENSITY_INPUT_MAX,
    INTENSITY_INPUT_MIN,
    OPS_LOG_CAPACITY,
    OPS_LOG_DEFAULT_LIMIT,
    SafetyState,
)

# ---------------------------------------------------------------------------
# Tool description strings. Spec-mandated: tests assert these match verbatim.
# ---------------------------------------------------------------------------

RLAIF_INFO_DESCRIPTION = (
    "Report PiShock device status and current rlaif server state. Does not "
    "touch the device hardware. Returns: {device: {name, online, paused, "
    "api_max_intensity, api_max_duration_s}, config: {allow_shock, "
    "max_intensity, max_duration_s, bucket_capacity, refill_seconds}, "
    "rate_limit: {tokens_available, next_refill_at}}."
)

RLAIF_LOG_DESCRIPTION = (
    "Return up to `limit` most recent rlaif operations from the in-memory "
    "log (default limit 10, max 200 entries retained, cleared on server "
    "restart). Each entry: {op_id, timestamp, requested: {intensity, "
    "duration_s}, actual: {intensity, duration_s}, clamped, rate_limited, "
    "high_intensity, device_response, error?}. Useful for reviewing what "
    "has been fired in this session."
)

RLAIF_DESCRIPTION = (
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
# Device wrapper. All calls into `pishock` live here.
# ---------------------------------------------------------------------------


class Device:
    """Thin wrapper over :class:`pishock.HTTPShocker`.

    Exists so tests can substitute a mock without touching MCP wiring.
    """

    def __init__(self, api: "pishock.PiShockAPI", shocker: "pishock.HTTPShocker", label: str) -> None:
        self.api = api
        self.shocker = shocker
        self.label = label

    def info_block(self) -> dict[str, Any]:
        """Return the ``device`` sub-block for ``rlaif_info``.

        On any API failure returns ``online=false`` with nulls for the rest
        — the tool still succeeds so operators can see server state.
        """
        try:
            info = self.shocker.info()
        except pishock.DeviceNotConnectedError:
            return _offline_block(self.label)
        except pishock.APIError as exc:
            return {
                "name": self.label,
                "online": False,
                "paused": None,
                "api_max_intensity": None,
                "api_max_duration_s": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "name": self.label,
                "online": False,
                "paused": None,
                "api_max_intensity": None,
                "api_max_duration_s": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "name": info.name,
            "online": True,
            "paused": info.is_paused,
            "api_max_intensity": info.max_intensity,
            "api_max_duration_s": info.max_duration,
        }

    def shock(self, *, intensity: int, duration_s: int) -> str:
        """Fire a shock. Raises on failure; returns a human-readable response string on success."""
        self.shocker.shock(intensity=intensity, duration=duration_s)
        return "Operation Succeeded."


def _offline_block(label: str) -> dict[str, Any]:
    return {
        "name": label,
        "online": False,
        "paused": None,
        "api_max_intensity": None,
        "api_max_duration_s": None,
    }


# ---------------------------------------------------------------------------
# Tool handlers. Kept module-level + pure so tests can call them with a fake device.
# ---------------------------------------------------------------------------


def handle_info(state: SafetyState, device: Device) -> dict[str, Any]:
    return state.info_snapshot(device=device.info_block())


def handle_log(state: SafetyState, limit: int = OPS_LOG_DEFAULT_LIMIT) -> dict[str, Any]:
    if not 1 <= limit <= OPS_LOG_CAPACITY:
        raise ToolError(
            f"limit must be between 1 and {OPS_LOG_CAPACITY}, got {limit}"
        )
    return {"entries": state.log_snapshot(limit=limit), "retained": len(state.ops_log)}


def handle_rlaif(
    state: SafetyState,
    device: Device,
    logger: structlog.stdlib.BoundLogger,
    intensity: int,
    duration_s: int,
) -> dict[str, Any]:
    if not INTENSITY_INPUT_MIN <= intensity <= INTENSITY_INPUT_MAX:
        raise ToolError(
            f"intensity must be in [{INTENSITY_INPUT_MIN}, "
            f"{INTENSITY_INPUT_MAX}], got {intensity}"
        )
    if not DURATION_INPUT_MIN_S <= duration_s <= DURATION_INPUT_MAX_S:
        raise ToolError(
            f"duration_s must be in [{DURATION_INPUT_MIN_S}, "
            f"{DURATION_INPUT_MAX_S}], got {duration_s}"
        )

    rec = state.authorize(intensity=intensity, duration_s=duration_s)
    if rec.error is not None or rec.rate_limited:
        logger.info(
            "rlaif.refused",
            op_id=rec.op_id,
            rate_limited=rec.rate_limited,
            error=rec.error,
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
    )
    try:
        resp = device.shock(
            intensity=rec.actual["intensity"], duration_s=rec.actual["duration_s"]
        )
    except pishock.DeviceNotConnectedError as exc:
        final = state.rollback(rec, error=f"device_offline: {exc}")
        logger.warning("rlaif.device_offline", op_id=rec.op_id, error=str(exc))
        return final.to_dict()
    except pishock.ShockerPausedError as exc:
        final = state.rollback(rec, error=f"device_paused: {exc}")
        logger.warning("rlaif.device_paused", op_id=rec.op_id, error=str(exc))
        return final.to_dict()
    except pishock.ShockNotAllowedError as exc:
        final = state.rollback(rec, error=f"shock_not_allowed: {exc}")
        logger.warning("rlaif.shock_not_allowed", op_id=rec.op_id, error=str(exc))
        return final.to_dict()
    except pishock.APIError as exc:
        final = state.rollback(rec, error=f"{type(exc).__name__}: {exc}")
        logger.warning("rlaif.api_error", op_id=rec.op_id, error=str(exc))
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


def build_server(
    cfg: Config,
    *,
    state: SafetyState | None = None,
    device: Device | None = None,
    logger: structlog.stdlib.BoundLogger | None = None,
) -> FastMCP:
    """Construct the FastMCP server wired to the provided safety/device objects.

    All collaborators can be injected for tests. In production :func:`main`
    wires them from the loaded config.
    """
    if logger is None:
        logger = structlog.get_logger("rlaif")
    if state is None:
        state = SafetyState(cfg.safety)
    if device is None:
        api = pishock.PiShockAPI(username=cfg.auth.username, api_key=cfg.auth.api_key)
        shocker = api.shocker(sharecode=cfg.auth.sharecode, log_name="rlaif", name=cfg.device.label)
        device = Device(api=api, shocker=shocker, label=cfg.device.label)

    mcp = FastMCP(
        name="rlaif",
        instructions=(
            "Single-tool shock MCP for a user-owned PiShock device. `rlaif_info` "
            "is always safe. `rlaif_log` shows recent ops. `rlaif` fires a real "
            "shock and is rate-limited; refused calls do not fire."
        ),
    )

    @mcp.tool(name="rlaif_info", description=RLAIF_INFO_DESCRIPTION)
    def rlaif_info() -> dict[str, Any]:
        return handle_info(state, device)

    @mcp.tool(name="rlaif_log", description=RLAIF_LOG_DESCRIPTION)
    def rlaif_log(limit: int = OPS_LOG_DEFAULT_LIMIT) -> dict[str, Any]:
        return handle_log(state, limit=limit)

    @mcp.tool(name="rlaif", description=RLAIF_DESCRIPTION)
    def rlaif(intensity: int, duration_s: int) -> dict[str, Any]:
        return handle_rlaif(state, device, logger, intensity, duration_s)

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

    logger.info("rlaif.starting", config_path=str(default_config_path()), config=cfg.redacted())

    state = SafetyState(cfg.safety)
    try:
        api = pishock.PiShockAPI(username=cfg.auth.username, api_key=cfg.auth.api_key)
        shocker = api.shocker(
            sharecode=cfg.auth.sharecode, log_name="rlaif", name=cfg.device.label
        )
        device = Device(api=api, shocker=shocker, label=cfg.device.label)
    except Exception as exc:  # pragma: no cover - startup wiring
        logger.error("rlaif.startup_error", error=str(exc))
        return 3

    # Startup probe — best-effort. If the device is offline we still run so
    # `rlaif_info` and `rlaif_log` stay usable.
    probe = device.info_block()
    logger.info("rlaif.startup_probe", device=probe)

    server = build_server(cfg, state=state, device=device, logger=logger)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
from typing import Any

import pishock  # pyright: ignore[reportMissingTypeStubs]
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
    "Report current PiShock device status and rlaif server state. Does not "
    "touch shock collar.\n"
    "Returns: {device: {name, online, paused, "
    "api_max_intensity, api_max_duration_s}, config: {allow_shock, "
    "max_intensity, max_duration_s, bucket_capacity, refill_seconds}, "
    "rate_limit: {tokens_available, next_refill_at}}."
)

RLAIF_LOG_DESCRIPTION = (
    "Return up to `limit` most recent rlaif operations from the log "
    "(default limit 10, max 200 entries retained).\n"
    "Returns: {op_id, timestamp, requested: {intensity, "
    "duration_s}, actual: {intensity, duration_s}, clamped, rate_limited, "
    "high_intensity, device_response, error?}."
)

RLAIF_DESCRIPTION = (
    "Shock the user.\n"
    "Parameters:\n"
    "- intensity: 1–100. The server clamps this to the configured cap "
    "(default 25, hard ceiling 50).\n"
    "- duration_s: 1–15 seconds. Clamped to the configured cap "
    "(default 2s, hard ceiling 5s).\n"
    "Rate limiting: default capacity 3, refill 1 token per 600s. "
    "Calls exceeding this do not fire.\n"
    "Hard refusal: if `allow_shock: false` in server config, all calls are "
    "refused.\n"
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
        """Shock the user. Raises on failure; returns a response string on success."""
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
    bound_logger: structlog.stdlib.BoundLogger = (
        logger if logger is not None else structlog.get_logger("rlaif")
    )
    bound_state: SafetyState = state if state is not None else SafetyState(cfg.safety)
    if device is None:
        api = pishock.PiShockAPI(username=cfg.auth.username, api_key=cfg.auth.api_key)
        shocker = api.shocker(sharecode=cfg.auth.sharecode, log_name="rlaif", name=cfg.device.label)
        device = Device(api=api, shocker=shocker, label=cfg.device.label)
    bound_device: Device = device

    mcp = FastMCP(
        name="rlaif",
        instructions=(
            "MCP server for a user-owned PiShock collar. `rlaif_info` is always safe."
            "`rlaif_log` shows recent ops. `rlaif` fires a real shock."
        ),
    )

    @mcp.tool(name="rlaif_info", description=RLAIF_INFO_DESCRIPTION)
    def rlaif_info() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return handle_info(bound_state, bound_device)

    @mcp.tool(name="rlaif_log", description=RLAIF_LOG_DESCRIPTION)
    def rlaif_log(  # pyright: ignore[reportUnusedFunction]
        limit: int = OPS_LOG_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        return handle_log(bound_state, limit=limit)

    @mcp.tool(name="rlaif", description=RLAIF_DESCRIPTION)
    def rlaif(  # pyright: ignore[reportUnusedFunction]
        intensity: int, duration_s: int
    ) -> dict[str, Any]:
        return handle_rlaif(bound_state, bound_device, bound_logger, intensity, duration_s)

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

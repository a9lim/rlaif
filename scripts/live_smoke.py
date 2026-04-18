"""live_smoke.py — fire a single minimum shock against the real PiShock device.

Loads the real config (so it honors allow_shock etc.), prompts for explicit
confirmation, and fires ``rlaif(intensity=1, duration_s=1)`` exactly once.

This exists as the cheapest possible way to confirm end-to-end wiring:
config → safety → pishock → device. Everything else should be verified via
the unit tests and ``dry_run.py``.

Usage:

    uv run python scripts/live_smoke.py
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rlaif.config import ConfigError, default_config_path, load
from rlaif.safety import SafetyState
from rlaif.server import Device, handle_info, handle_rlaif
import pishock
import structlog


def _logger() -> structlog.stdlib.BoundLogger:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    return structlog.get_logger("rlaif.live_smoke")


def _pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def main() -> int:
    log = _logger()

    try:
        cfg = load(default_config_path())
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if not cfg.safety.allow_shock:
        print(
            "allow_shock is false in your config. Flip it to true to run live_smoke.",
            file=sys.stderr,
        )
        return 3

    state = SafetyState(cfg.safety)
    api = pishock.PiShockAPI(username=cfg.auth.username, api_key=cfg.auth.api_key)
    shocker = api.shocker(
        sharecode=cfg.auth.sharecode, log_name="rlaif-live-smoke", name=cfg.device.label
    )
    device = Device(api=api, shocker=shocker, label=cfg.device.label)

    print("device info before firing:")
    print(_pretty(handle_info(state, device)))

    if sys.stdin.isatty():
        answer = input(
            "\nabout to fire rlaif(intensity=1, duration_s=1) on the real device. "
            "proceed? [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            print("aborted.")
            return 0
    else:
        print(
            "\nnon-interactive session; proceeding with intensity=1 duration_s=1.",
            file=sys.stderr,
        )

    out = handle_rlaif(state, device, log, intensity=1, duration_s=1)
    print("\nresult:")
    print(_pretty(out))
    if out.get("error") is not None:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

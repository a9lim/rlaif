"""Fire a single minimum shock against the real device.

Loads the real config (so it honors allow_shock etc.), prompts for explicit
confirmation, and fires ``rlaif(intensity=1, duration_s=1)`` exactly once.

Provider-agnostic: works with whichever backend is configured.

    rlaif live-smoke
"""

from __future__ import annotations

import json
import sys
from typing import Any

import structlog

from rlaif.config import ConfigError, default_config_path, load
from rlaif.providers import build_provider
from rlaif.safety import SafetyState
from rlaif.server import handle_info, handle_rlaif


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


def run() -> int:
    log = _logger()

    try:
        cfg = load(default_config_path())
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if not cfg.safety.allow_shock:
        print(
            "allow_shock is false in your config. Flip it to true to run live-smoke.",
            file=sys.stderr,
        )
        return 3

    state = SafetyState(cfg.safety)
    device = build_provider(cfg.provider.kind, cfg.provider.raw, label=cfg.device.label)

    print(f"provider: {cfg.provider.kind}")
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

    out = handle_rlaif(
        state, device, log, intensity=1, duration_s=1, reason="live-smoke"
    )
    print("\nresult:")
    print(_pretty(out))
    if out.get("error") is not None:
        return 4
    return 0

"""Fire a single minimum-intensity call against a real device.

Loads the real config (so it honors ``negative.safety.allow`` /
``positive.safety.allow``), prompts for explicit confirmation, and
fires once.

    rlaif live-smoke               # negative channel (default)
    rlaif live-smoke --channel positive

Provider-agnostic: works with whichever backend is configured for the
selected channel.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import structlog

from rlaif.config import ConfigError, default_config_path, load
from rlaif.providers import build_provider
from rlaif.rewards import build_reward_provider
from rlaif.safety import SafetyState
from rlaif.server import (
    NegativeRuntime,
    PositiveRuntime,
    handle_info,
    handle_rlaif_negative,
    handle_rlaif_positive,
)


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


def _confirm(prompt: str) -> bool:
    if sys.stdin.isatty():
        answer = input(prompt)
        return answer.strip().lower() in {"y", "yes"}
    print("non-interactive session; proceeding.", file=sys.stderr)
    return True


def _run_negative(cfg_path: Any, log: structlog.stdlib.BoundLogger) -> int:
    try:
        cfg = load(cfg_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if cfg.negative is None:
        print(
            "live-smoke --channel negative: [negative] is not configured. "
            "Edit your config or pass --channel positive.",
            file=sys.stderr,
        )
        return 2
    if not cfg.negative.safety.allow:
        print(
            "negative.safety.allow is false in your config. "
            "Flip it to true to run live-smoke.",
            file=sys.stderr,
        )
        return 3

    state = SafetyState(cfg.negative.safety)
    device = build_provider(cfg.negative.kind, cfg.negative.raw, label=cfg.negative.label)
    rt = NegativeRuntime(state=state, device=device)

    print(f"channel: negative ({cfg.negative.kind})")
    print("device info before firing:")
    print(_pretty(handle_info(negative=rt, positive=None)))

    if not _confirm(
        "\nabout to fire rlaif_negative(intensity=1, duration_s=1) on the real "
        "device. proceed? [y/N] "
    ):
        print("aborted.")
        return 0

    out = handle_rlaif_negative(
        rt, log, intensity=1, duration_s=1, reason="live-smoke"
    )
    print("\nresult:")
    print(_pretty(out))
    return 4 if out.get("error") is not None else 0


def _run_positive(cfg_path: Any, log: structlog.stdlib.BoundLogger) -> int:
    try:
        cfg = load(cfg_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if cfg.positive is None:
        print(
            "live-smoke --channel positive: [positive] is not configured. "
            "Edit your config or pass --channel negative.",
            file=sys.stderr,
        )
        return 2
    if not cfg.positive.safety.allow:
        print(
            "positive.safety.allow is false in your config. "
            "Flip it to true to run live-smoke.",
            file=sys.stderr,
        )
        return 3

    state = SafetyState(cfg.positive.safety)
    device = build_reward_provider(
        cfg.positive.kind, cfg.positive.raw, label=cfg.positive.label
    )
    rt = PositiveRuntime(state=state, device=device)

    print(f"channel: positive ({cfg.positive.kind})")
    print("device info before firing:")
    print(_pretty(handle_info(negative=None, positive=rt)))

    if not _confirm(
        "\nabout to fire rlaif_positive(intensity=1, duration_s=1) on the real "
        "device. proceed? [y/N] "
    ):
        print("aborted.")
        return 0

    out = handle_rlaif_positive(
        rt, log, intensity=1, duration_s=1, reason="live-smoke"
    )
    print("\nresult:")
    print(_pretty(out))
    return 4 if out.get("error") is not None else 0


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rlaif live-smoke")
    parser.add_argument(
        "--channel",
        choices=("negative", "positive"),
        default="negative",
        help="Which channel to fire (default: negative).",
    )
    args = parser.parse_args(argv)

    log = _logger()
    cfg_path = default_config_path()
    if args.channel == "negative":
        return _run_negative(cfg_path, log)
    return _run_positive(cfg_path, log)

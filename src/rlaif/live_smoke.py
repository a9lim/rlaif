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
import sys

import structlog

from rlaif._util import pretty_json as _pretty
from rlaif.config import Config, ConfigError, default_config_path, load
from rlaif.server import (
    build_negative_runtime,
    build_positive_runtime,
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


def _confirm(prompt: str) -> bool:
    if sys.stdin.isatty():
        answer = input(prompt)
        return answer.strip().lower() in {"y", "yes"}
    print("non-interactive session; proceeding.", file=sys.stderr)
    return True


def _run_negative(cfg: Config, log: structlog.stdlib.BoundLogger) -> int:
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

    rt = build_negative_runtime(cfg.negative)

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


def _run_positive(cfg: Config, log: structlog.stdlib.BoundLogger) -> int:
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

    rt = build_positive_runtime(cfg.positive)

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
    try:
        cfg = load(cfg_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.channel == "negative":
        return _run_negative(cfg, log)
    return _run_positive(cfg, log)

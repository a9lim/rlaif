"""Read-only health check for rlaif.

Loads configured credentials for any present channel, builds the
configured providers, prints the same snapshot ``rlaif_info`` would
return, and surfaces any issues. Does not fire any device.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rlaif.config import ConfigError, default_config_path, load
from rlaif.server import (
    NegativeRuntime,
    PositiveRuntime,
    build_negative_runtime,
    build_positive_runtime,
    handle_info,
)


def _offline_hint(channel: str, kind: str) -> str:
    if channel == "negative":
        if kind == "pishock":
            return (
                "negative: device.online is false — check pishock.com, your "
                "sharecode, and that the device isn't paused"
            )
        return (
            "negative: device.online is false — check your api_token, "
            "shocker_id, and that the shocker isn't paused on the "
            "OpenShock dashboard"
        )
    return (
        "positive: device.online is false — check that Intiface Central "
        "is running and the gateway sees your device"
    )


def _channel_issues(
    snap: dict[str, Any], *, channel: str, offline_hint: str
) -> list[str]:
    issues: list[str] = []
    dev = snap["device"]
    if not dev.get("online"):
        issues.append(offline_hint)
    if dev.get("paused"):
        issues.append(f"{channel}: device is paused at the provider")
    if not snap["config"]["allow"]:
        issues.append(
            f"{channel}.safety.allow=false — rlaif will refuse every "
            f"rlaif_{channel} call until you flip it"
        )
    if dev.get("error"):
        issues.append(f"{channel}: device probe error: {dev['error']}")
    return issues


def run() -> int:
    cfg_path = default_config_path()
    try:
        cfg = load(cfg_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        print(f"  (expected at {cfg_path} — run `rlaif init`)", file=sys.stderr)
        return 2

    snapshot: dict[str, Any] = {}
    issues: list[str] = []

    n_rt: NegativeRuntime | None = None
    p_rt: PositiveRuntime | None = None

    if cfg.negative is not None:
        try:
            n_rt = build_negative_runtime(cfg.negative)
        except Exception as exc:
            print(
                f"could not build {cfg.negative.kind} provider: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 3

    if cfg.positive is not None:
        try:
            p_rt = build_positive_runtime(cfg.positive)
        except Exception as exc:
            print(
                f"could not build {cfg.positive.kind} provider: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 3

    snapshot = handle_info(negative=n_rt, positive=p_rt)
    if cfg.negative is not None:
        snapshot["negative"]["provider"] = cfg.negative.kind
    if cfg.positive is not None:
        snapshot["positive"]["provider"] = cfg.positive.kind

    print(json.dumps(snapshot, indent=2, default=str))

    if cfg.negative is not None:
        issues.extend(
            _channel_issues(
                snapshot["negative"],
                channel="negative",
                offline_hint=_offline_hint("negative", cfg.negative.kind),
            )
        )
    if cfg.positive is not None:
        issues.extend(
            _channel_issues(
                snapshot["positive"],
                channel="positive",
                offline_hint=_offline_hint("positive", cfg.positive.kind),
            )
        )

    if issues:
        print("\nissues:")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("\nhealthy.")
    return 0

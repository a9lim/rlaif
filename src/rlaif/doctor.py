"""Read-only health check for rlaif.

Loads configured credentials for any present channel, builds the
configured providers, prints the same snapshot ``rlaif_info`` would
return, and surfaces any issues. Does not fire any device.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rlaif.config import ChannelConfig, ConfigError, default_config_path, load
from rlaif.providers import build_provider
from rlaif.rewards import build_reward_provider
from rlaif.safety import SafetyState
from rlaif.server import (
    NegativeRuntime,
    PositiveRuntime,
    handle_info,
)


def _negative_issues(snap: dict[str, Any], kind: str) -> list[str]:
    issues: list[str] = []
    dev = snap["device"]
    if not dev.get("online"):
        if kind == "pishock":
            issues.append(
                "negative: device.online is false — check pishock.com, your "
                "sharecode, and that the device isn't paused"
            )
        else:
            issues.append(
                "negative: device.online is false — check your api_token, "
                "shocker_id, and that the shocker isn't paused on the "
                "OpenShock dashboard"
            )
    if dev.get("paused"):
        issues.append("negative: device is paused at the provider")
    if not snap["config"]["allow"]:
        issues.append(
            "negative.safety.allow=false — rlaif will refuse every "
            "rlaif_negative call until you flip it"
        )
    if dev.get("error"):
        issues.append(f"negative: device probe error: {dev['error']}")
    return issues


def _positive_issues(snap: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    dev = snap["device"]
    if not dev.get("online"):
        issues.append(
            "positive: device.online is false — check that Intiface Central "
            "is running and the gateway sees your device"
        )
    if dev.get("paused"):
        issues.append("positive: device is paused at the provider")
    if not snap["config"]["allow"]:
        issues.append(
            "positive.safety.allow=false — rlaif will refuse every "
            "rlaif_positive call until you flip it"
        )
    if dev.get("error"):
        issues.append(f"positive: device probe error: {dev['error']}")
    return issues


def _build_negative_runtime(cc: ChannelConfig) -> NegativeRuntime | str:
    state = SafetyState(cc.safety)
    try:
        device = build_provider(cc.kind, cc.raw, label=cc.label)
    except Exception as exc:
        return f"could not build {cc.kind} provider: {type(exc).__name__}: {exc}"
    return NegativeRuntime(state=state, device=device)


def _build_positive_runtime(cc: ChannelConfig) -> PositiveRuntime | str:
    state = SafetyState(cc.safety)
    try:
        device = build_reward_provider(cc.kind, cc.raw, label=cc.label)
    except Exception as exc:
        return f"could not build {cc.kind} provider: {type(exc).__name__}: {exc}"
    return PositiveRuntime(state=state, device=device)


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
        result = _build_negative_runtime(cfg.negative)
        if isinstance(result, str):
            print(result, file=sys.stderr)
            return 3
        n_rt = result

    if cfg.positive is not None:
        result = _build_positive_runtime(cfg.positive)
        if isinstance(result, str):
            print(result, file=sys.stderr)
            return 3
        p_rt = result

    snapshot = handle_info(negative=n_rt, positive=p_rt)
    if cfg.negative is not None:
        snapshot["negative"]["provider"] = cfg.negative.kind
    if cfg.positive is not None:
        snapshot["positive"]["provider"] = cfg.positive.kind

    print(json.dumps(snapshot, indent=2, default=str))

    if cfg.negative is not None:
        issues.extend(_negative_issues(snapshot["negative"], cfg.negative.kind))
    if cfg.positive is not None:
        issues.extend(_positive_issues(snapshot["positive"]))

    if issues:
        print("\nissues:")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("\nhealthy.")
    return 0

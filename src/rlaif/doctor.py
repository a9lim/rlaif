"""Read-only health check for rlaif.

Loads the configured credentials, builds the configured provider, prints
the same snapshot ``rlaif_info`` would return, and surfaces any issues.
Does not fire the device.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rlaif.config import ConfigError, default_config_path, load
from rlaif.providers import Provider, build_provider
from rlaif.safety import SafetyState
from rlaif.server import handle_info


def run() -> int:
    cfg_path = default_config_path()
    try:
        cfg = load(cfg_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        print(f"  (expected at {cfg_path} — run `rlaif init`)", file=sys.stderr)
        return 2

    state = SafetyState(cfg.safety)
    try:
        device: Provider = build_provider(
            cfg.provider.kind, cfg.provider.raw, label=cfg.device.label
        )
    except Exception as exc:
        print(
            f"could not build {cfg.provider.kind} provider: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 3

    snapshot: dict[str, Any] = handle_info(state, device)
    snapshot["provider"] = cfg.provider.kind
    print(json.dumps(snapshot, indent=2, default=str))

    issues: list[str] = []
    if not snapshot["device"]["online"]:
        if cfg.provider.kind == "pishock":
            issues.append(
                "device.online is false — check pishock.com, your sharecode, "
                "and that the device isn't paused"
            )
        else:
            issues.append(
                "device.online is false — check your api_token, shocker_id, "
                "and that the shocker isn't paused on the OpenShock dashboard"
            )
    if snapshot["device"].get("paused"):
        issues.append("device is paused at the provider")
    if not snapshot["config"]["allow_shock"]:
        issues.append(
            "allow_shock=false — rlaif will refuse every shock call until you flip it"
        )
    dev_err = snapshot["device"].get("error")
    if dev_err:
        issues.append(f"device probe error: {dev_err}")

    if issues:
        print("\nissues:")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("\nhealthy.")
    return 0

"""Read-only health check for rlaif.

Loads the configured credentials, probes the PiShock device, prints the same
snapshot ``rlaif_info`` would return, and surfaces any issues. Does not fire
the device.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pishock  # pyright: ignore[reportMissingTypeStubs]

from rlaif.config import ConfigError, default_config_path, load
from rlaif.safety import SafetyState
from rlaif.server import Device, handle_info


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
        api = pishock.PiShockAPI(username=cfg.auth.username, api_key=cfg.auth.api_key)
        shocker = api.shocker(
            sharecode=cfg.auth.sharecode,
            log_name="rlaif-doctor",
            name=cfg.device.label,
        )
        device = Device(api=api, shocker=shocker, label=cfg.device.label)
    except Exception as exc:
        print(
            f"could not build pishock client: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 3

    snapshot: dict[str, Any] = handle_info(state, device)
    print(json.dumps(snapshot, indent=2, default=str))

    issues: list[str] = []
    if not snapshot["device"]["online"]:
        issues.append(
            "device.online is false — check pishock.com, your sharecode, "
            "and that the device isn't paused"
        )
    if snapshot["device"].get("paused"):
        issues.append("device is paused on pishock.com")
    if not snapshot["config"]["allow_shock"]:
        issues.append(
            "allow_shock=false — rlaif will refuse every shock call until you flip it"
        )

    if issues:
        print("\nissues:")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("\nhealthy.")
    return 0

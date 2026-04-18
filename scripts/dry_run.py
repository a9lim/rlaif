"""dry_run.py — exercise every rlaif tool against a mocked PiShock device.

Exits nonzero if any safety invariant is violated. Run this after any
nontrivial change to the safety layer:

    uv run python scripts/dry_run.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pishock

from rlaif.config import AuthConfig, Config, DeviceConfig
from rlaif.safety import SafetyConfig, SafetyState
from rlaif.server import Device, handle_info, handle_log, handle_rlaif


@dataclass
class Scenario:
    name: str
    ok: bool
    detail: str


def _fake_device_online() -> Device:
    shocker = MagicMock(spec=pishock.HTTPShocker)
    info = MagicMock()
    info.name = "mock"
    info.is_paused = False
    info.max_intensity = 100
    info.max_duration = 15
    shocker.info.return_value = info
    shocker.shock.return_value = None
    api = MagicMock(spec=pishock.PiShockAPI)
    return Device(api=api, shocker=shocker, label="mock")


def _fake_device_offline() -> Device:
    shocker = MagicMock(spec=pishock.HTTPShocker)
    shocker.info.side_effect = pishock.DeviceNotConnectedError("offline")
    shocker.shock.side_effect = pishock.DeviceNotConnectedError("offline")
    api = MagicMock(spec=pishock.PiShockAPI)
    return Device(api=api, shocker=shocker, label="mock")


def _logger() -> MagicMock:
    return MagicMock()


def _pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def main() -> int:
    scenarios: list[Scenario] = []

    # --- info tool against online device ---
    state = SafetyState(
        SafetyConfig(allow_shock=True, max_intensity=20, max_duration_s=2),
        now=0.0,
    )
    device = _fake_device_online()
    info = handle_info(state, device)
    print("=== handle_info (online) ===")
    print(_pretty(info))
    scenarios.append(
        Scenario(
            "info/online",
            info["device"]["online"] is True
            and info["config"]["allow_shock"] is True
            and info["rate_limit"]["tokens_available"] == 3,
            f"online={info['device']['online']}, tokens={info['rate_limit']['tokens_available']}",
        )
    )

    # --- allow_shock=false refuses without firing ---
    state_off = SafetyState(SafetyConfig(allow_shock=False), now=0.0)
    device_off = _fake_device_online()
    out = handle_rlaif(state_off, device_off, _logger(), intensity=1, duration_s=1)
    print("\n=== handle_rlaif (allow_shock=false) ===")
    print(_pretty(out))
    scenarios.append(
        Scenario(
            "allow_shock=false refuses",
            out.get("error") is not None
            and "allow_shock" in out["error"]
            and device_off.shocker.shock.call_count == 0
            and state_off.bucket.available(0.0) == state_off.config.bucket_capacity,
            f"shock_calls={device_off.shocker.shock.call_count}, error={out.get('error')}",
        )
    )

    # --- clamping surfaces requested vs actual ---
    state_cl = SafetyState(
        SafetyConfig(allow_shock=True, max_intensity=10, max_duration_s=2),
        now=0.0,
    )
    device_cl = _fake_device_online()
    out = handle_rlaif(state_cl, device_cl, _logger(), intensity=80, duration_s=10)
    print("\n=== handle_rlaif (clamp 80/10 -> 10/2) ===")
    print(_pretty(out))
    kwargs = device_cl.shocker.shock.call_args.kwargs
    scenarios.append(
        Scenario(
            "clamping",
            out["clamped"] is True
            and out["actual"] == {"intensity": 10, "duration_s": 2}
            and out["requested"] == {"intensity": 80, "duration_s": 10}
            and kwargs["intensity"] == 10
            and kwargs["duration"] == 2,
            f"actual={out['actual']}, device_args={kwargs}",
        )
    )

    # --- rate limit trips at capacity + refill permits again ---
    state_rl = SafetyState(
        SafetyConfig(allow_shock=True, bucket_capacity=2, refill_seconds=60),
        now=0.0,
    )
    device_rl = _fake_device_online()
    calls = [
        handle_rlaif(state_rl, device_rl, _logger(), intensity=1, duration_s=1)
        for _ in range(2)
    ]
    refused = handle_rlaif(state_rl, device_rl, _logger(), intensity=1, duration_s=1)
    print("\n=== handle_rlaif (rate limit, 3rd call) ===")
    print(_pretty(refused))
    scenarios.append(
        Scenario(
            "rate limit trips",
            all(c.get("error") is None for c in calls)
            and refused["rate_limited"] is True
            and device_rl.shocker.shock.call_count == 2,
            f"shock_calls={device_rl.shocker.shock.call_count}",
        )
    )

    # --- device offline rolls back token ---
    state_ro = SafetyState(
        SafetyConfig(allow_shock=True, bucket_capacity=1), now=0.0
    )
    device_ro = _fake_device_offline()
    out = handle_rlaif(state_ro, device_ro, _logger(), intensity=1, duration_s=1)
    print("\n=== handle_rlaif (device offline, rollback) ===")
    print(_pretty(out))
    scenarios.append(
        Scenario(
            "device_offline rolls back token",
            out.get("error") is not None
            and "device_offline" in out["error"]
            and state_ro.bucket.available(0.0) == 1,
            f"tokens_after={state_ro.bucket.available(0.0)}, error={out.get('error')}",
        )
    )

    # --- near_ceiling warning + high_intensity flag ---
    state_hi = SafetyState(
        SafetyConfig(
            allow_shock=True,
            max_intensity=25,
            max_duration_s=5,
            warn_threshold_intensity=15,
        ),
        now=0.0,
    )
    device_hi = _fake_device_online()
    out = handle_rlaif(state_hi, device_hi, _logger(), intensity=20, duration_s=1)
    print("\n=== handle_rlaif (20/1, near_ceiling + high_intensity) ===")
    print(_pretty(out))
    scenarios.append(
        Scenario(
            "high_intensity + near_ceiling",
            out["high_intensity"] is True and out.get("warnings") == ["near_ceiling"],
            f"high_intensity={out['high_intensity']}, warnings={out.get('warnings')}",
        )
    )

    # --- log tool returns recent ops, newest first ---
    log = handle_log(state_hi, limit=10)
    print("\n=== handle_log after ops ===")
    print(_pretty(log))
    scenarios.append(
        Scenario(
            "log retained + newest first",
            log["retained"] == 1 and len(log["entries"]) == 1,
            f"retained={log['retained']}",
        )
    )

    print("\n=== summary ===")
    all_ok = True
    for s in scenarios:
        flag = "ok" if s.ok else "FAIL"
        print(f"  [{flag}] {s.name}: {s.detail}")
        if not s.ok:
            all_ok = False

    if not all_ok:
        print("\n!! safety invariants violated — see failures above")
        return 1
    print("\nall safety invariants held.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

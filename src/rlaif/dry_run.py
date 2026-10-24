"""Exercise every rlaif tool against mock providers on both channels.

Exits nonzero if any safety invariant is violated. Run after any nontrivial
change to the safety layer:

    rlaif dry-run

Provider-agnostic: uses :class:`MockProvider` and
:class:`MockRewardProvider`, so no real account or device is required.

Runs on a synthetic clock: the fire gate holds for each burst, so
scenarios that fire more than once advance the clock past the holdoff
instead of sleeping through it.
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

from rlaif import safety
from rlaif._util import pretty_json as _pretty
from rlaif.providers import DeviceOfflineError
from rlaif.providers.mock import MockProvider
from rlaif.rewards import RewardWatchdogError
from rlaif.rewards.mock import MockRewardProvider
from rlaif.safety import (
    BURST_HOLDOFF_MARGIN_S,
    NEGATIVE_CHANNEL,
    POSITIVE_CHANNEL,
    FireGate,
    SafetyConfig,
    SafetyState,
)
from rlaif.server import (
    NegativeRuntime,
    PositiveRuntime,
    handle_info,
    handle_log,
    handle_rlaif_negative,
    handle_rlaif_positive,
)


@dataclass
class Scenario:
    name: str
    ok: bool
    detail: str


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@contextmanager
def _synthetic_clock() -> Generator[_Clock]:
    """Point the safety layer's monotonic and wall clocks at one fake."""
    clock = _Clock()
    with patch.object(safety, "_monotonic", clock), patch.object(safety, "_wall", clock):
        yield clock


def _logger() -> MagicMock:
    return MagicMock()


def _negative_state(gate: FireGate | None = None, **kw: Any) -> SafetyState:
    return SafetyState(SafetyConfig(spec=NEGATIVE_CHANNEL, **kw), gate=gate, now=0.0)


def _positive_state(gate: FireGate | None = None, **kw: Any) -> SafetyState:
    return SafetyState(SafetyConfig(spec=POSITIVE_CHANNEL, **kw), gate=gate, now=0.0)


def _run_negative(scenarios: list[Scenario], clock: _Clock) -> None:
    """Exercise every negative-channel invariant against a mock provider."""
    state = _negative_state(allow=True, max_intensity=20, max_duration_s=2)
    device = MockProvider(label="mock", api_max_intensity=100, api_max_duration_s=15)
    rt = NegativeRuntime(state=state, device=device)
    info = handle_info(negative=rt, positive=None)
    print("=== negative: handle_info (online) ===")
    print(_pretty(info))
    scenarios.append(
        Scenario(
            "negative info/online",
            info["negative"]["device"]["online"] is True
            and info["negative"]["config"]["allow"] is True
            and info["negative"]["rate_limit"]["tokens_available"] == 3,
            f"online={info['negative']['device']['online']}, "
            f"tokens={info['negative']['rate_limit']['tokens_available']}",
        )
    )

    state_off = _negative_state(allow=False)
    device_off = MockProvider()
    rt_off = NegativeRuntime(state=state_off, device=device_off)
    out = handle_rlaif_negative(rt_off, _logger(), intensity=1, duration_s=1)
    print("\n=== negative: handle_rlaif_negative (allow=false) ===")
    print(_pretty(out))
    scenarios.append(
        Scenario(
            "negative allow=false refuses",
            out.get("error") is not None
            and "negative.safety.allow" in out["error"]
            and len(device_off.calls) == 0
            and rt_off.state.bucket.available(0.0) == rt_off.state.config.bucket_capacity,
            f"shock_calls={len(device_off.calls)}, error={out.get('error')}",
        )
    )

    state_cl = _negative_state(allow=True, max_intensity=10, max_duration_s=2)
    device_cl = MockProvider()
    rt_cl = NegativeRuntime(state=state_cl, device=device_cl)
    out = handle_rlaif_negative(rt_cl, _logger(), intensity=80, duration_s=10)
    print("\n=== negative: clamp 80/10 -> 10/2 ===")
    print(_pretty(out))
    scenarios.append(
        Scenario(
            "negative clamping",
            out["clamped"] is True
            and out["actual"] == {"intensity": 10, "duration_s": 2}
            and out["requested"] == {"intensity": 80, "duration_s": 10}
            and device_cl.calls == [(10, 2)],
            f"actual={out['actual']}, calls={device_cl.calls}",
        )
    )

    state_rl = _negative_state(allow=True, bucket_capacity=2, refill_seconds=60)
    device_rl = MockProvider()
    rt_rl = NegativeRuntime(state=state_rl, device=device_rl)
    calls: list[dict[str, Any]] = []
    for _ in range(2):
        calls.append(handle_rlaif_negative(rt_rl, _logger(), intensity=1, duration_s=1))
        clock.t += 10.0  # past the fire gate's holdoff, inside the refill interval
    refused = handle_rlaif_negative(rt_rl, _logger(), intensity=1, duration_s=1)
    print("\n=== negative: rate limit, 3rd call ===")
    print(_pretty(refused))
    scenarios.append(
        Scenario(
            "negative rate limit trips",
            all(c.get("error") is None for c in calls)
            and refused["rate_limited"] is True
            and len(device_rl.calls) == 2,
            f"shock_calls={len(device_rl.calls)}",
        )
    )

    state_ro = _negative_state(allow=True, bucket_capacity=1)
    device_ro = MockProvider(shock_error=DeviceOfflineError("offline"))
    rt_ro = NegativeRuntime(state=state_ro, device=device_ro)
    out = handle_rlaif_negative(rt_ro, _logger(), intensity=1, duration_s=1)
    print("\n=== negative: device offline, rollback ===")
    print(_pretty(out))
    scenarios.append(
        Scenario(
            "negative device_offline rolls back token",
            out.get("error") is not None
            and "device_offline" in out["error"]
            and rt_ro.state.bucket.available(0.0) == 1,
            f"tokens_after={rt_ro.state.bucket.available(0.0)}, error={out.get('error')}",
        )
    )

    state_hi = _negative_state(
        allow=True,
        max_intensity=25,
        max_duration_s=5,
        warn_threshold_intensity=15,
    )
    rt_hi = NegativeRuntime(state=state_hi, device=MockProvider())
    out = handle_rlaif_negative(rt_hi, _logger(), intensity=20, duration_s=1, reason="dry-run audit")
    print("\n=== negative: 20/1, near_ceiling + high_intensity + reason ===")
    print(_pretty(out))
    scenarios.append(
        Scenario(
            "negative high_intensity + near_ceiling + reason",
            out["high_intensity"] is True
            and out.get("warnings") == ["near_ceiling"]
            and out.get("reason") == "dry-run audit"
            and out["channel"] == "negative",
            f"high_intensity={out['high_intensity']}, "
            f"warnings={out.get('warnings')}, "
            f"reason={out.get('reason')}, "
            f"channel={out.get('channel')}",
        )
    )


def _run_positive(scenarios: list[Scenario]) -> None:
    """Exercise every positive-channel invariant against a mock reward provider."""
    state = _positive_state(allow=True, max_intensity=80, max_duration_s=10)
    device = MockRewardProvider(label="mock-vibe", actuators=2)
    rt = PositiveRuntime(state=state, device=device)
    info = handle_info(negative=None, positive=rt)
    print("\n=== positive: handle_info (online) ===")
    print(_pretty(info))
    scenarios.append(
        Scenario(
            "positive info/online",
            info["positive"]["device"]["online"] is True
            and info["positive"]["device"]["actuators"] == 2
            and info["positive"]["config"]["allow"] is True,
            f"online={info['positive']['device']['online']}, actuators={info['positive']['device']['actuators']}",
        )
    )

    state_off = _positive_state(allow=False)
    device_off = MockRewardProvider()
    rt_off = PositiveRuntime(state=state_off, device=device_off)
    out = handle_rlaif_positive(rt_off, _logger(), intensity=1, duration_s=1)
    print("\n=== positive: handle_rlaif_positive (allow=false) ===")
    print(_pretty(out))
    scenarios.append(
        Scenario(
            "positive allow=false refuses",
            out.get("error") is not None and "positive.safety.allow" in out["error"] and len(device_off.calls) == 0,
            f"calls={len(device_off.calls)}, error={out.get('error')}",
        )
    )

    state_wd = _positive_state(allow=True, bucket_capacity=2)
    device_wd = MockRewardProvider(vibrate_error=RewardWatchdogError("safety stop did not deliver"))
    rt_wd = PositiveRuntime(state=state_wd, device=device_wd)
    out = handle_rlaif_positive(rt_wd, _logger(), intensity=1, duration_s=1)
    print("\n=== positive: watchdog (no token refund) ===")
    print(_pretty(out))
    scenarios.append(
        Scenario(
            "positive watchdog does NOT refund token",
            out.get("error") is not None
            and "watchdog" in out["error"]
            # Capacity was 2; consumed 1 and watchdog did not refund.
            and rt_wd.state.bucket.available(0.0) == 1,
            f"tokens_after={rt_wd.state.bucket.available(0.0)}",
        )
    )

    # Cross-check: the reward channel accepts inputs the negative channel
    # would reject (intensity 90, duration 30s) without complaint, because
    # its ceilings are higher.
    state_rng = _positive_state(allow=True, max_intensity=100, max_duration_s=30)
    device_rng = MockRewardProvider()
    rt_rng = PositiveRuntime(state=state_rng, device=device_rng)
    out = handle_rlaif_positive(rt_rng, _logger(), intensity=90, duration_s=30, reason="extended praise")
    print("\n=== positive: 90/30 (would fail on negative) ===")
    print(_pretty(out))
    scenarios.append(
        Scenario(
            "positive accepts wider input range",
            out.get("error") is None
            and out["actual"] == {"intensity": 90, "duration_s": 30}
            and device_rng.calls == [(90, 30)]
            and out["channel"] == "positive",
            f"actual={out['actual']}, calls={device_rng.calls}",
        )
    )


def _run_fire_gate(scenarios: list[Scenario], clock: _Clock) -> None:
    """One stimulus at a time, server-wide: busy refusals, never queued."""
    gate = FireGate()
    n_dev = MockProvider()
    p_dev = MockRewardProvider()
    n_rt = NegativeRuntime(state=_negative_state(gate, allow=True, max_duration_s=2), device=n_dev)
    p_rt = PositiveRuntime(state=_positive_state(gate, allow=True), device=p_dev)

    first = handle_rlaif_negative(n_rt, _logger(), intensity=1, duration_s=2)
    again = handle_rlaif_negative(n_rt, _logger(), intensity=1, duration_s=2)
    print("\n=== fire gate: back-to-back shock ===")
    print(_pretty(again))
    scenarios.append(
        Scenario(
            "back-to-back shock refused busy, no token spent",
            first.get("error") is None
            and str(again.get("error", "")).startswith("busy:")
            and len(n_dev.calls) == 1
            and n_rt.state.bucket.available(clock.t) == n_rt.state.config.bucket_capacity - 1,
            f"shock_calls={len(n_dev.calls)}, error={again.get('error')}",
        )
    )

    crossed = handle_rlaif_positive(p_rt, _logger(), intensity=10, duration_s=1)
    scenarios.append(
        Scenario(
            "shock in progress blocks vibration (shared gate)",
            "negative stimulus is in progress" in str(crossed.get("error", "")) and p_dev.calls == [],
            f"vibrate_calls={len(p_dev.calls)}, error={crossed.get('error')}",
        )
    )

    clock.t += 2 + BURST_HOLDOFF_MARGIN_S
    vibe = handle_rlaif_positive(p_rt, _logger(), intensity=10, duration_s=1)
    scenarios.append(
        Scenario(
            "gate reopens after burst + margin",
            vibe.get("error") is None and p_dev.calls == [(10, 1)],
            f"vibrate_calls={len(p_dev.calls)}, error={vibe.get('error')}",
        )
    )

    # Concurrent callers on both channels (SDK 2.x runs sync handlers on
    # worker threads): exactly one device call in total.
    clock.t += 100.0
    race_gate = FireGate()
    race_n = MockProvider()
    race_p = MockRewardProvider()
    race_n_rt = NegativeRuntime(state=_negative_state(race_gate, allow=True), device=race_n)
    race_p_rt = PositiveRuntime(state=_positive_state(race_gate, allow=True), device=race_p)
    n_threads = 16
    barrier = threading.Barrier(n_threads)
    outcomes: list[dict[str, Any]] = []
    outcomes_lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        if i % 2 == 0:
            out = handle_rlaif_negative(race_n_rt, _logger(), intensity=1, duration_s=1)
        else:
            out = handle_rlaif_positive(race_p_rt, _logger(), intensity=1, duration_s=1)
        with outcomes_lock:
            outcomes.append(out)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    fired = len(race_n.calls) + len(race_p.calls)
    busy = sum(1 for o in outcomes if str(o.get("error", "")).startswith("busy:"))
    scenarios.append(
        Scenario(
            "concurrent fire attempts: exactly one reaches a device",
            fired == 1 and busy == n_threads - 1,
            f"device_calls={fired}, busy_refusals={busy}",
        )
    )


def _run_combined_log(scenarios: list[Scenario], clock: _Clock) -> None:
    """The combined log interleaves both channels by timestamp."""
    gate = FireGate()
    n_state = _negative_state(gate, allow=True)
    n_rt = NegativeRuntime(state=n_state, device=MockProvider())
    p_state = _positive_state(gate, allow=True)
    p_rt = PositiveRuntime(state=p_state, device=MockRewardProvider())

    outs: list[dict[str, Any]] = []
    outs.append(handle_rlaif_negative(n_rt, _logger(), intensity=1, duration_s=1, reason="neg-1"))
    clock.t += 10.0
    outs.append(handle_rlaif_positive(p_rt, _logger(), intensity=1, duration_s=1, reason="pos-1"))
    clock.t += 10.0
    outs.append(handle_rlaif_negative(n_rt, _logger(), intensity=1, duration_s=1, reason="neg-2"))

    log = handle_log(negative=n_rt, positive=p_rt, limit=10)
    print("\n=== combined log (newest first, both channels) ===")
    print(_pretty(log))
    channels = [e["channel"] for e in log["entries"]]
    scenarios.append(
        Scenario(
            "combined log includes both channels",
            all(o.get("error") is None for o in outs)
            and log["retained"] == 3
            and channels == ["negative", "positive", "negative"]
            and log["entries"][0].get("reason") == "neg-2",
            f"retained={log['retained']}, channels={channels}",
        )
    )


def run() -> int:
    scenarios: list[Scenario] = []
    with _synthetic_clock() as clock:
        _run_negative(scenarios, clock)
        _run_positive(scenarios)
        _run_fire_gate(scenarios, clock)
        _run_combined_log(scenarios, clock)

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

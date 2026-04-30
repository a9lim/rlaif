"""In-memory mock reward provider for tests and ``rlaif dry-run``.

Behaviour is controlled by attributes after construction:

    p = MockRewardProvider(label="mock")
    p.online = False                          # info() reports offline
    p.vibrate_error = RewardDeviceOfflineError(...)  # next vibrate() raises this

Each ``vibrate()`` call appends a ``(intensity, duration_s)`` tuple to
``p.calls``. Tests can assert on call shape without touching network.
"""

from __future__ import annotations

from typing import Any

from rlaif.rewards.base import (
    RewardDeviceInfo,
    RewardProvider,
    RewardProviderError,
)


class MockRewardProvider(RewardProvider):
    def __init__(
        self,
        *,
        label: str = "mock-reward",
        online: bool = True,
        paused: bool = False,
        actuators: int = 1,
        info_error: str | None = None,
        vibrate_error: RewardProviderError | None = None,
        response: str = "Operation Succeeded.",
    ) -> None:
        self.label = label
        self.online = online
        self.paused = paused
        self.actuators = actuators
        self.info_error = info_error
        self.vibrate_error = vibrate_error
        self.response = response
        self.calls: list[tuple[int, int]] = []

    @classmethod
    def from_config(cls, raw: dict[str, Any], *, label: str) -> MockRewardProvider:
        return cls(label=label)

    def info(self) -> RewardDeviceInfo:
        if self.info_error is not None:
            return RewardDeviceInfo(
                name=self.label,
                online=False,
                actuators=None,
                paused=None,
                error=self.info_error,
            )
        if not self.online:
            return RewardDeviceInfo(
                name=self.label,
                online=False,
                actuators=None,
                paused=None,
            )
        return RewardDeviceInfo(
            name=self.label,
            online=True,
            actuators=self.actuators,
            paused=self.paused,
        )

    def vibrate(self, *, intensity: int, duration_s: int) -> str:
        self.calls.append((intensity, duration_s))
        if self.vibrate_error is not None:
            raise self.vibrate_error
        return self.response

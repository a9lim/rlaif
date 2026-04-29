"""In-memory mock provider for tests and ``rlaif dry-run``.

Behaviour is controlled by attributes after construction:

    p = MockProvider(label="mock")
    p.online = False                       # info() reports offline
    p.shock_error = DeviceOfflineError(...) # next shock() raises this

Each ``shock()`` call appends a ``(intensity, duration_s)`` tuple to
``p.calls``. Tests can assert on call shape without touching network.
"""

from __future__ import annotations

from typing import Any

from rlaif.providers.base import DeviceInfo, Provider, ProviderError


class MockProvider(Provider):
    def __init__(
        self,
        *,
        label: str = "mock",
        online: bool = True,
        paused: bool = False,
        api_max_intensity: int = 100,
        api_max_duration_s: int = 15,
        info_error: str | None = None,
        shock_error: ProviderError | None = None,
        response: str = "Operation Succeeded.",
    ) -> None:
        self.label = label
        self.online = online
        self.paused = paused
        self.api_max_intensity = api_max_intensity
        self.api_max_duration_s = api_max_duration_s
        self.info_error = info_error
        self.shock_error = shock_error
        self.response = response
        self.calls: list[tuple[int, int]] = []

    @classmethod
    def from_config(cls, raw: dict[str, Any], *, label: str) -> MockProvider:
        return cls(label=label)

    def info(self) -> DeviceInfo:
        if self.info_error is not None:
            return DeviceInfo(
                name=self.label,
                online=False,
                paused=None,
                api_max_intensity=None,
                api_max_duration_s=None,
                error=self.info_error,
            )
        if not self.online:
            return DeviceInfo(
                name=self.label,
                online=False,
                paused=None,
                api_max_intensity=None,
                api_max_duration_s=None,
            )
        return DeviceInfo(
            name=self.label,
            online=True,
            paused=self.paused,
            api_max_intensity=self.api_max_intensity,
            api_max_duration_s=self.api_max_duration_s,
        )

    def shock(self, *, intensity: int, duration_s: int) -> str:
        self.calls.append((intensity, duration_s))
        if self.shock_error is not None:
            raise self.shock_error
        return self.response

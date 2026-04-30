"""PiShock backend (https://pishock.com).

Wraps the upstream ``pishock`` package and normalizes its exception
taxonomy onto :mod:`rlaif.providers.base`.

Auth: per-account ``username`` + ``api_token`` (the value pishock.com
calls the "API key" at https://pishock.com/#/account), plus a per-device
``shocker_id`` (the value pishock.com calls the "share code"). The field
names match OpenShock's so both backends share a single vocabulary; only
the upstream pishock SDK still sees the legacy ``api_key`` / ``sharecode``
parameter names.

Capability ranges as the upstream API enforces them:
* intensity: 1–100
* duration: 1–15 s

The safety layer's caps are typically much tighter than these.
"""

from __future__ import annotations

from typing import Any

import pishock as _pishock  # pyright: ignore[reportMissingTypeStubs]

from rlaif.providers.base import (
    DeviceInfo,
    DeviceOfflineError,
    DevicePausedError,
    Provider,
    ProviderAuthError,
    ProviderError,
    ShockNotAllowedError,
)

# Upstream API ceilings — informational only, surfaced via ``DeviceInfo``.
PISHOCK_API_MAX_INTENSITY: int = 100
PISHOCK_API_MAX_DURATION_S: int = 15

# The Intiface and OpenShock backends both reach the gateway as "rlaif"; do
# the same on PiShock so log lines on every provider's dashboard agree on
# who fired the call.
_PISHOCK_LOG_NAME: str = "rlaif"


class PiShockProvider(Provider):
    """PiShock HTTP shocker provider."""

    def __init__(
        self,
        *,
        username: str,
        api_token: str,
        shocker_id: str,
        label: str = "pishock",
    ) -> None:
        self.label = label
        # The upstream pishock SDK still uses the legacy parameter names;
        # only our config/CLI surface speaks `api_token` / `shocker_id`.
        self._api: Any = _pishock.PiShockAPI(username=username, api_key=api_token)
        self._shocker: Any = self._api.shocker(sharecode=shocker_id, log_name=_PISHOCK_LOG_NAME, name=label)

    @classmethod
    def from_config(cls, raw: dict[str, str], *, label: str) -> PiShockProvider:
        try:
            return cls(
                username=raw["username"],
                api_token=raw["api_token"],
                shocker_id=raw["shocker_id"],
                label=label,
            )
        except KeyError as exc:
            raise ValueError(f"pishock provider missing required field: {exc.args[0]}") from exc

    @classmethod
    def from_components(cls, *, api: Any, shocker: Any, label: str) -> PiShockProvider:
        """Build a provider from already-constructed pishock objects.

        Used by tests to inject mocks; production code uses ``from_config``.
        """
        inst = cls.__new__(cls)
        inst.label = label
        inst._api = api  # pyright: ignore[reportPrivateUsage]
        inst._shocker = shocker  # pyright: ignore[reportPrivateUsage]
        return inst

    def info(self) -> DeviceInfo:
        try:
            data: Any = self._shocker.info()
        except _pishock.DeviceNotConnectedError:
            return DeviceInfo(
                name=self.label,
                online=False,
                paused=None,
                api_max_intensity=None,
                api_max_duration_s=None,
            )
        except _pishock.NotAuthorizedError as exc:
            return DeviceInfo(
                name=self.label,
                online=False,
                paused=None,
                api_max_intensity=None,
                api_max_duration_s=None,
                error=f"NotAuthorizedError: {exc}",
            )
        except _pishock.APIError as exc:
            return DeviceInfo(
                name=self.label,
                online=False,
                paused=None,
                api_max_intensity=None,
                api_max_duration_s=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception as exc:  # pragma: no cover - defensive
            return DeviceInfo(
                name=self.label,
                online=False,
                paused=None,
                api_max_intensity=None,
                api_max_duration_s=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        return DeviceInfo(
            name=str(data.name),
            online=True,
            paused=bool(data.is_paused),
            api_max_intensity=int(data.max_intensity),
            api_max_duration_s=int(data.max_duration),
        )

    def shock(self, *, intensity: int, duration_s: int) -> str:
        try:
            self._shocker.shock(intensity=intensity, duration=duration_s)
        except _pishock.DeviceNotConnectedError as exc:
            raise DeviceOfflineError(str(exc)) from exc
        except _pishock.ShockerPausedError as exc:
            raise DevicePausedError(str(exc)) from exc
        except _pishock.ShockNotAllowedError as exc:
            raise ShockNotAllowedError(str(exc)) from exc
        except _pishock.NotAuthorizedError as exc:
            raise ProviderAuthError(str(exc)) from exc
        except _pishock.APIError as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc
        # pishock.HTTPShocker.shock returns None on success; we synthesize a
        # human-readable string so the response field is always populated.
        return "Operation Succeeded."

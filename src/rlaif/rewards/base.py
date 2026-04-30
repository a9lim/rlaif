"""Reward provider base types: ABC, error taxonomy, capability descriptor.

Mirrors :mod:`rlaif.providers.base` for the reward channel. Kept in a
parallel namespace on purpose — a code change cannot cross-wire shock and
reward providers when the types live in disjoint modules.

Error taxonomy:

* :class:`RewardDeviceOfflineError` — device disconnected from the gateway
  (Intiface lost the BLE link, or the controller process died). Token is
  refunded.
* :class:`RewardProviderAuthError` — WebSocket handshake refused, or the
  gateway demanded credentials we don't have. Token is refunded.
* :class:`RewardWatchdogError` — the disconnect watchdog tripped before
  the explicit stop reached the device. Surfaced as an ops-log error so
  the operator can inspect what happened. Token is NOT refunded — a
  watchdog event means the device may still be running, and we want the
  rate limit to slow the agent down until the operator confirms.
* :class:`RewardProviderError` — anything else; token is refunded.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class RewardProviderError(Exception):
    """Base for all reward-side provider failures."""


class RewardProviderAuthError(RewardProviderError):
    """Gateway authentication or authorization failed."""


class RewardDeviceOfflineError(RewardProviderError):
    """Device is not reachable from the gateway."""


class RewardWatchdogError(RewardProviderError):
    """Disconnect watchdog tripped — safety stop did not reach the device.

    Raised when the explicit stop after ``duration_s`` could not be
    delivered (gateway WS dropped, device unresponsive, etc). Means the
    device may still be running; operator intervention may be required.
    """


@dataclass(frozen=True)
class RewardDeviceInfo:
    """Snapshot returned by :meth:`RewardProvider.info`.

    ``online`` is best-effort: for Intiface it means the WebSocket is
    open and the gateway is enumerating at least the requested device.
    The physical BLE link to the toy can still drop between info() and
    vibrate() — the ground truth is firing a control and watching for
    :class:`RewardDeviceOfflineError`.

    ``actuators`` is the number of independently-controllable vibration
    motors on the selected device, as the gateway reports them. Most
    consumer toys are 1; some have multiple.
    """

    name: str
    online: bool
    actuators: int | None
    paused: bool | None = None
    error: str | None = None


class RewardProvider(ABC):
    """Abstract reward (vibration) device backend.

    Concrete providers ship a ``from_config`` classmethod and the two
    instance methods below. The server only ever sees this surface.
    """

    label: str

    @abstractmethod
    def info(self) -> RewardDeviceInfo:
        """Probe device state. MUST NOT trigger the device.

        On any backend failure, return a :class:`RewardDeviceInfo` with
        ``online=False`` and an ``error`` string — do not raise. The
        ``rlaif_info`` tool stays usable even when the gateway is down so
        the operator can still inspect server-side state.
        """

    @abstractmethod
    def vibrate(self, *, intensity: int, duration_s: int) -> str:
        """Trigger one vibration burst. Raises a typed
        :class:`RewardProviderError` subclass on failure; returns a short
        human-readable response string on success.

        ``intensity`` is 1–100 (the safety layer clamps to its caps before
        we get here). ``duration_s`` is 1–60 seconds. Providers MUST
        guarantee that the device stops at the end of ``duration_s`` even
        if the controller process is killed mid-burst — this is the
        disconnect-watchdog contract documented in :class:`RewardWatchdogError`.
        """

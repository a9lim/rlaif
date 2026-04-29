"""Provider base types: ABC, error taxonomy, capability descriptor.

The safety layer is provider-blind. Everything below the shock tool calls
into a :class:`Provider`; the only contract is the methods on this class
and the exception types defined here.

Error taxonomy:

* :class:`DeviceOfflineError` — device is not reachable. The safety layer
  refunds the token and the operator should retry once the device comes back.
* :class:`DevicePausedError` — the backend reports the device paused
  (provider-side pause, not rlaif's ``allow_shock``). Token is refunded.
* :class:`ShockNotAllowedError` — backend refused this specific call
  (capability cap, ToS, share permission). Token is NOT refunded — the
  caller already knew the inputs were within rlaif's caps; this is the
  backend overriding us, and we treat it like a hard refusal.
* :class:`ProviderAuthError` — credentials wrong/expired. Token is refunded
  because retry-after-fix is the right operator action.
* :class:`ProviderError` — anything else; token is refunded.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderError(Exception):
    """Base for all provider-side failures."""


class ProviderAuthError(ProviderError):
    """Authentication or authorization failed (401/403)."""


class DeviceOfflineError(ProviderError):
    """Device is not reachable from the backend."""


class DevicePausedError(ProviderError):
    """Backend reports the device paused (separate from ``allow_shock``)."""


class ShockNotAllowedError(ProviderError):
    """Backend refused this specific call (capability cap, share permission)."""


@dataclass(frozen=True)
class DeviceInfo:
    """Snapshot returned by ``Provider.info()``.

    Mirrors the ``device`` sub-block in the ``rlaif_info`` tool response.

    ``online`` is a best-effort reachability flag — for some providers it
    only tells you the API call succeeded, not that the physical device is
    actually online. The ground truth is firing a control and checking for
    :class:`DeviceOfflineError`. The README documents this gotcha.
    """

    name: str
    online: bool
    paused: bool | None
    api_max_intensity: int | None
    api_max_duration_s: int | None
    error: str | None = None


class Provider(ABC):
    """Abstract shock device backend.

    Concrete providers ship two class methods (``from_config`` and the
    instance API below). The server only ever sees the instance API.
    """

    label: str

    @abstractmethod
    def info(self) -> DeviceInfo:
        """Probe device state. MUST NOT trigger the device.

        On any backend failure, return a :class:`DeviceInfo` with
        ``online=False`` and an ``error`` string — do not raise. The
        ``rlaif_info`` tool stays usable even when the backend is down so
        the operator can still inspect server-side state.
        """

    @abstractmethod
    def shock(self, *, intensity: int, duration_s: int) -> str:
        """Trigger one shock. Raises a typed :class:`ProviderError` subclass
        on failure; returns a short human-readable response string on success.

        ``intensity`` is 1–100. ``duration_s`` is 1–15 (the safety layer
        clamps to its caps before we get here, so this method does not need
        to re-validate). Providers convert units internally as needed.
        """

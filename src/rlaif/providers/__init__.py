"""Provider abstraction for shock devices.

Backends live behind :class:`Provider`. The server, doctor, and live-smoke
all import ``Provider`` (and its error taxonomy) from here and never reach
into a specific backend's SDK.

Built-in backends:

* :class:`rlaif.providers.pishock.PiShockProvider`
* :class:`rlaif.providers.openshock.OpenShockProvider`
* :class:`rlaif.providers.mock.MockProvider` (used by tests + ``rlaif dry-run``)

Third parties can register additional providers via the
``rlaif.providers`` setuptools entry-point group; see :func:`build_provider`.
"""

from __future__ import annotations

from rlaif.providers.base import (
    DeviceInfo,
    DeviceOfflineError,
    DevicePausedError,
    Provider,
    ProviderAuthError,
    ProviderError,
    ShockNotAllowedError,
)

__all__ = [
    "DeviceInfo",
    "DeviceOfflineError",
    "DevicePausedError",
    "Provider",
    "ProviderAuthError",
    "ProviderError",
    "ShockNotAllowedError",
    "build_provider",
]


def build_provider(kind: str, raw: dict[str, str], *, label: str) -> Provider:
    """Construct a provider by kind name.

    Built-in kinds: ``pishock``, ``openshock``, ``mock``. Unknown kinds raise
    :class:`ValueError` — the config layer turns this into a ``ConfigError``
    so the operator sees the available options.
    """
    if kind == "pishock":
        from rlaif.providers.pishock import PiShockProvider
        return PiShockProvider.from_config(raw, label=label)
    if kind == "openshock":
        from rlaif.providers.openshock import OpenShockProvider
        return OpenShockProvider.from_config(raw, label=label)
    if kind == "mock":
        from rlaif.providers.mock import MockProvider
        return MockProvider.from_config(raw, label=label)
    raise ValueError(
        f"unknown provider kind {kind!r}; built-in kinds: pishock, openshock, mock"
    )

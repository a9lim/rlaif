"""Reward provider abstraction for vibration / positive-reinforcement devices.

Backends live behind :class:`RewardProvider`. The server, doctor, and
live-smoke import :class:`RewardProvider` (and its error taxonomy) from
here and never reach into a specific backend's SDK.

Built-in backends:

* :class:`rlaif.rewards.intiface.IntifaceProvider`
* :class:`rlaif.rewards.mock.MockRewardProvider` (used by tests + dry-run)

Kept in a parallel namespace from :mod:`rlaif.providers` so a code change
cannot accidentally cross-wire shock and reward providers.
"""

from __future__ import annotations

from rlaif.rewards.base import (
    RewardDeviceInfo,
    RewardDeviceOfflineError,
    RewardProvider,
    RewardProviderAuthError,
    RewardProviderError,
    RewardWatchdogError,
)

__all__ = [
    "RewardDeviceInfo",
    "RewardDeviceOfflineError",
    "RewardProvider",
    "RewardProviderAuthError",
    "RewardProviderError",
    "RewardWatchdogError",
    "build_reward_provider",
]


def build_reward_provider(
    kind: str, raw: dict[str, str], *, label: str
) -> RewardProvider:
    """Construct a reward provider by kind name.

    Built-in kinds: ``intiface``, ``mock``. Unknown kinds raise
    :class:`ValueError` — the config layer turns this into a
    ``ConfigError`` so the operator sees the available options.
    """
    if kind == "intiface":
        from rlaif.rewards.intiface import IntifaceProvider
        return IntifaceProvider.from_config(raw, label=label)
    if kind == "mock":
        from rlaif.rewards.mock import MockRewardProvider
        return MockRewardProvider.from_config(raw, label=label)
    raise ValueError(
        f"unknown reward provider kind {kind!r}; built-in kinds: intiface, mock"
    )

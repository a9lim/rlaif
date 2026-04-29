"""Reward-provider-layer tests.

Mirrors :mod:`tests.test_providers` for the reward channel. The
:class:`MockRewardProvider` is the workhorse — exhaustive intiface tests
live in a separate module once the buttplug-py wrapper exists.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import pytest

from rlaif.rewards import (
    RewardDeviceInfo,
    RewardDeviceOfflineError,
    RewardProvider,
    RewardProviderAuthError,
    RewardProviderError,
    RewardWatchdogError,
    build_reward_provider,
)
from rlaif.rewards.mock import MockRewardProvider


# ---------------------------------------------------------------------------
# build_reward_provider dispatch
# ---------------------------------------------------------------------------


def test_build_reward_provider_mock() -> None:
    p = build_reward_provider("mock", {}, label="dev")
    assert isinstance(p, RewardProvider)
    assert isinstance(p, MockRewardProvider)
    assert p.label == "dev"


def test_build_reward_provider_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown reward provider"):
        build_reward_provider("totally-fake", {}, label="x")


# ---------------------------------------------------------------------------
# Error taxonomy hierarchy
# ---------------------------------------------------------------------------


class TestRewardErrorTaxonomy:
    def test_offline_is_provider_error(self) -> None:
        assert issubclass(RewardDeviceOfflineError, RewardProviderError)

    def test_auth_is_provider_error(self) -> None:
        assert issubclass(RewardProviderAuthError, RewardProviderError)

    def test_watchdog_is_provider_error(self) -> None:
        assert issubclass(RewardWatchdogError, RewardProviderError)

    def test_subclasses_are_distinct(self) -> None:
        # Catching RewardDeviceOfflineError must not match
        # RewardProviderAuthError, etc — the safety layer dispatches on the
        # specific subclass to decide whether to refund the token.
        assert not issubclass(RewardDeviceOfflineError, RewardProviderAuthError)
        assert not issubclass(RewardWatchdogError, RewardDeviceOfflineError)


# ---------------------------------------------------------------------------
# MockRewardProvider — info() shape, vibrate() recording
# ---------------------------------------------------------------------------


class TestMockRewardInfo:
    def test_default_online_with_actuators(self) -> None:
        p = MockRewardProvider(label="toy", actuators=2)
        info = p.info()
        assert isinstance(info, RewardDeviceInfo)
        assert info.online is True
        assert info.name == "toy"
        assert info.actuators == 2
        assert info.error is None

    def test_offline_does_not_raise(self) -> None:
        # Provider contract: info() never raises. An offline device returns
        # online=False so rlaif_info stays usable when the gateway is down.
        p = MockRewardProvider(online=False)
        info = p.info()
        assert info.online is False
        assert info.actuators is None

    def test_info_error_surfaces_in_response(self) -> None:
        p = MockRewardProvider(info_error="ws handshake refused")
        info = p.info()
        assert info.online is False
        assert info.error == "ws handshake refused"


class TestMockRewardVibrate:
    def test_records_call(self) -> None:
        p = MockRewardProvider()
        resp = p.vibrate(intensity=50, duration_s=2)
        assert resp == "Operation Succeeded."
        assert p.calls == [(50, 2)]

    def test_multiple_calls_accumulate(self) -> None:
        p = MockRewardProvider()
        p.vibrate(intensity=10, duration_s=1)
        p.vibrate(intensity=20, duration_s=2)
        assert p.calls == [(10, 1), (20, 2)]

    def test_offline_error_raises(self) -> None:
        p = MockRewardProvider(
            vibrate_error=RewardDeviceOfflineError("ble link dropped")
        )
        with pytest.raises(RewardDeviceOfflineError):
            p.vibrate(intensity=1, duration_s=1)
        # Call still recorded — tests can assert what was attempted.
        assert p.calls == [(1, 1)]

    def test_watchdog_error_raises(self) -> None:
        # The watchdog signal is the new-shape error compared to the shock
        # channel — make sure it can be raised cleanly through the provider
        # surface.
        p = MockRewardProvider(
            vibrate_error=RewardWatchdogError("safety stop did not deliver")
        )
        with pytest.raises(RewardWatchdogError):
            p.vibrate(intensity=1, duration_s=1)

    def test_auth_error_raises(self) -> None:
        p = MockRewardProvider(
            vibrate_error=RewardProviderAuthError("intiface refused our handshake")
        )
        with pytest.raises(RewardProviderAuthError):
            p.vibrate(intensity=1, duration_s=1)


class TestMockRewardFromConfig:
    def test_round_trip_via_build_reward_provider(self) -> None:
        # The mock provider ignores the config dict but the factory should
        # still accept and forward it without crashing.
        p = build_reward_provider("mock", {"anything": "ignored"}, label="lbl")
        assert isinstance(p, MockRewardProvider)
        assert p.label == "lbl"

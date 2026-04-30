"""Intiface provider tests.

The client is mocked end-to-end via a fake core that records the
async coroutines our provider hands to it. We never open a real WebSocket
in the unit suite; an end-to-end test against a live Intiface gateway is
the operator's job (`rlaif live-smoke --channel positive`).
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false, reportArgumentType=false

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from buttplug import (
    ButtplugClient,
    ButtplugConnectorError,
    ButtplugDeviceError,
    ButtplugHandshakeError,
    DeviceOutputCommand,
    OutputType,
)

from rlaif.rewards import (
    RewardDeviceInfo,
    RewardDeviceOfflineError,
    RewardProviderAuthError,
    RewardProviderError,
    RewardWatchdogError,
)
from rlaif.rewards.intiface import (
    IntifaceProvider,
    _intensity_to_scalar,
)

# ---------------------------------------------------------------------------
# Fake core. Tests inject this in place of the real _IntifaceCore so
# nothing touches an event loop or a WebSocket. The fake records the
# coroutines our provider awaits so we can assert on them.
# ---------------------------------------------------------------------------


class _FakeCore:
    def __init__(self, client: ButtplugClient | None = None) -> None:
        self.client = client
        self.connect_error: Exception | None = None
        self.runs: list[Any] = []

    def get_client(self) -> ButtplugClient:
        if self.connect_error is not None:
            raise self.connect_error
        if self.client is None:
            raise RewardProviderError("no client wired into fake core")
        return self.client

    def run(self, coro: Any, timeout: float = 5.0) -> Any:
        # Record what the provider asked us to await, then close the
        # coroutine to satisfy Python's "coroutine was never awaited"
        # warning. The actual mock of the underlying call already
        # happened on the buttplug-side mock (run_output etc); we only
        # need to forward its return value or raise its exception.
        self.runs.append(coro)
        try:
            coro.send(None)
        except StopIteration as si:
            return si.value
        except Exception:
            coro.close()
            raise
        else:
            coro.close()
            return None


def _make_device(
    *,
    name: str = "ToyName",
    index: int = 0,
    display_name: str | None = None,
    actuators: int = 1,
    run_output_error: Exception | None = None,
    stop_error: Exception | None = None,
) -> MagicMock:
    """Build a MagicMock that mirrors the ButtplugDevice surface we touch."""
    device = MagicMock()
    device.name = name
    device.index = index
    device.display_name = display_name
    device.run_output = AsyncMock(side_effect=run_output_error)
    device.stop = AsyncMock(side_effect=stop_error)
    # get_features_with_output(VIBRATE) returns a list whose length is
    # the actuator count; the elements themselves do not matter to our
    # logic.
    device.get_features_with_output = MagicMock(return_value=[MagicMock()] * actuators)
    return device


def _make_client(devices: list[MagicMock] | None = None, *, connected: bool = True) -> MagicMock:
    client = MagicMock(spec=ButtplugClient)
    client.connected = connected
    client.devices = {d.index: d for d in (devices or [])}
    return client


def _provider(core: _FakeCore, **kw: Any) -> IntifaceProvider:
    return IntifaceProvider(
        label="test-vibe",
        base_url="ws://localhost:12345",
        core=core,
        **kw,
    )


# ---------------------------------------------------------------------------
# Intensity mapping
# ---------------------------------------------------------------------------


class TestIntensityToScalar:
    def test_min_intensity_floors_at_one_percent(self) -> None:
        # rlaif input 1/100 maps to a perceptible 0.01 scalar (not 0.0,
        # which the gateway treats as "stop").
        assert _intensity_to_scalar(1) == pytest.approx(0.01)

    def test_max_intensity_caps_at_one(self) -> None:
        assert _intensity_to_scalar(100) == pytest.approx(1.0)

    def test_midpoint(self) -> None:
        assert _intensity_to_scalar(50) == pytest.approx(0.5)

    def test_above_max_clamps(self) -> None:
        # Defensive: safety layer should clamp before we get here, but
        # if it ever doesn't we still hand the gateway a legal value.
        assert _intensity_to_scalar(150) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------


class TestDeviceSelection:
    def test_no_devices_visible_raises_offline(self) -> None:
        core = _FakeCore(client=_make_client(devices=[]))
        p = _provider(core)
        info = p.info()
        assert info.online is False
        assert info.error is not None
        assert "Intiface Central" in info.error

    def test_first_device_selected_by_default(self) -> None:
        d0 = _make_device(name="A", index=0)
        d1 = _make_device(name="B", index=1)
        core = _FakeCore(client=_make_client(devices=[d0, d1]))
        p = _provider(core)
        info = p.info()
        assert info.online is True
        assert info.name == "A"

    def test_device_name_selects(self) -> None:
        d0 = _make_device(name="GenericVibe", index=0)
        d1 = _make_device(name="Lovense Domi", index=1)
        core = _FakeCore(client=_make_client(devices=[d0, d1]))
        p = _provider(core, device_name="Lovense Domi")
        info = p.info()
        assert info.online is True
        assert info.name == "Lovense Domi"

    def test_device_name_matches_display_name(self) -> None:
        d0 = _make_device(name="ble-device-0", index=0, display_name="my toy")
        core = _FakeCore(client=_make_client(devices=[d0]))
        p = _provider(core, device_name="my toy")
        info = p.info()
        assert info.online is True

    def test_device_name_missing_raises_offline(self) -> None:
        d0 = _make_device(name="Other", index=0)
        core = _FakeCore(client=_make_client(devices=[d0]))
        p = _provider(core, device_name="Lovense Hush")
        info = p.info()
        assert info.online is False
        assert "Lovense Hush" in (info.error or "")


# ---------------------------------------------------------------------------
# info()
# ---------------------------------------------------------------------------


class TestInfo:
    def test_online_with_actuators_count(self) -> None:
        d = _make_device(name="MultiMotor", actuators=3)
        core = _FakeCore(client=_make_client(devices=[d]))
        info = _provider(core).info()
        assert isinstance(info, RewardDeviceInfo)
        assert info.online is True
        assert info.actuators == 3

    def test_connect_failure_returns_offline(self) -> None:
        # The provider's contract is that info() never raises; backend
        # failures surface as online=False with an error string.
        core = _FakeCore()
        core.connect_error = RewardDeviceOfflineError("intiface unreachable at ws://...")
        info = _provider(core).info()
        assert info.online is False
        assert info.error is not None
        assert "unreachable" in info.error

    def test_auth_error_surfaces_as_offline(self) -> None:
        # Auth failures also come back as online=False; the operator
        # sees the underlying message.
        core = _FakeCore()
        core.connect_error = RewardProviderAuthError("intiface handshake refused: bad name")
        info = _provider(core).info()
        assert info.online is False
        assert "handshake" in (info.error or "")


# ---------------------------------------------------------------------------
# vibrate()
# ---------------------------------------------------------------------------


class TestVibrateHappyPath:
    def test_sends_start_then_stop_with_correct_scalar(self) -> None:
        device = _make_device()
        core = _FakeCore(client=_make_client(devices=[device]))
        with patch("rlaif.rewards.intiface.time.sleep") as mock_sleep:
            resp = _provider(core).vibrate(intensity=50, duration_s=2)
        assert resp == "Operation Succeeded."
        # Sleep matches duration_s exactly.
        mock_sleep.assert_called_once_with(2)
        # Two RPCs: start then stop.
        assert device.run_output.await_count == 1
        assert device.stop.await_count == 1
        start_cmd: DeviceOutputCommand = device.run_output.await_args.args[0]
        assert start_cmd.output_type is OutputType.VIBRATE
        assert start_cmd.value == pytest.approx(0.5)

    def test_intensity_floored_for_minimum_input(self) -> None:
        device = _make_device()
        core = _FakeCore(client=_make_client(devices=[device]))
        with patch("rlaif.rewards.intiface.time.sleep"):
            _provider(core).vibrate(intensity=1, duration_s=1)
        cmd: DeviceOutputCommand = device.run_output.await_args.args[0]
        # 1/100 = 0.01, and we floor at 0.01 — matches.
        assert cmd.value == pytest.approx(0.01)


class TestVibrateConnectFailures:
    def test_connect_drop_raises_offline(self) -> None:
        # The connect step itself fails before we even reach the device.
        core = _FakeCore()
        core.connect_error = RewardDeviceOfflineError("intiface unreachable")
        with pytest.raises(RewardDeviceOfflineError):
            _provider(core).vibrate(intensity=10, duration_s=1)

    def test_connector_error_during_start_raises_offline(self) -> None:
        # Connect succeeds, then the WS drops mid-RPC during the start.
        device = _make_device(run_output_error=ButtplugConnectorError("ws closed mid-rpc"))
        core = _FakeCore(client=_make_client(devices=[device]))
        with patch("rlaif.rewards.intiface.time.sleep"):
            with pytest.raises(RewardDeviceOfflineError, match="dropped during start"):
                _provider(core).vibrate(intensity=10, duration_s=1)
        # Stop was never called because the start failed first.
        assert device.stop.await_count == 0

    def test_device_error_during_start_raises_provider_error(self) -> None:
        # Device exists but cannot vibrate (e.g. an LED-only toy).
        device = _make_device(run_output_error=ButtplugDeviceError("no vibrate features"))
        core = _FakeCore(client=_make_client(devices=[device]))
        with pytest.raises(RewardProviderError, match="cannot vibrate"):
            _provider(core).vibrate(intensity=10, duration_s=1)


class TestVibrateWatchdog:
    def test_stop_failure_raises_watchdog_error(self) -> None:
        # Start succeeded, sleep ran, but the explicit stop did not
        # deliver. This is the case where the device may still be
        # running; the safety layer must not refund the token.
        device = _make_device(stop_error=ButtplugConnectorError("ws closed"))
        core = _FakeCore(client=_make_client(devices=[device]))
        with patch("rlaif.rewards.intiface.time.sleep"):
            with pytest.raises(RewardWatchdogError, match="explicit stop did not deliver"):
                _provider(core).vibrate(intensity=10, duration_s=1)
        # Both RPCs were attempted, even though the stop blew up.
        assert device.run_output.await_count == 1
        assert device.stop.await_count == 1


# ---------------------------------------------------------------------------
# from_config
# ---------------------------------------------------------------------------


class TestFromConfig:
    def test_round_trips_credential_dict(self) -> None:
        # Use patch to swap out _IntifaceCore so from_config doesn't
        # actually try to start a thread or open a WS.
        with patch("rlaif.rewards.intiface._IntifaceCore") as fake_core_cls:
            fake_core_cls.return_value = _FakeCore()
            p = IntifaceProvider.from_config(
                {
                    "base_url": "ws://10.0.0.5:12345",
                    "device_name": "Lovense Domi",
                },
                label="lbl",
            )
        assert p.label == "lbl"
        assert p._device_name == "Lovense Domi"  # type: ignore[reportPrivateUsage]

    def test_defaults_applied_when_fields_missing(self) -> None:
        with patch("rlaif.rewards.intiface._IntifaceCore") as fake_core_cls:
            fake_core_cls.return_value = _FakeCore()
            p = IntifaceProvider.from_config({}, label="x")
        # base_url defaults to localhost; the device-name selector stays None.
        assert p._device_name is None  # type: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Connect-error mapping at the core level
# ---------------------------------------------------------------------------


class TestCoreConnectErrorMapping:
    """The core's get_client() must map upstream buttplug exceptions onto
    rlaif's normalized taxonomy. Tests stub _connect_async to raise the
    upstream exception, then verify the mapped one comes out."""

    def _build_core_with_failing_connect(self, exc: Exception) -> Any:
        from rlaif.rewards.intiface import _IntifaceCore

        core = _IntifaceCore.__new__(_IntifaceCore)
        # Manual minimal init — we don't actually want a thread or atexit.
        core._base_url = "ws://localhost:12345"  # type: ignore[reportPrivateUsage]
        core._client = None  # type: ignore[reportPrivateUsage]
        import threading

        core._connect_lock = threading.Lock()  # type: ignore[reportPrivateUsage]
        core._shutdown_started = False  # type: ignore[reportPrivateUsage]

        def fake_run(coro: Any, timeout: float = 5.0) -> Any:  # noqa: ARG001
            # Close the coroutine before raising so we don't get a
            # "coroutine was never awaited" warning during teardown.
            coro.close()
            raise exc

        core.run = fake_run  # type: ignore[method-assign]
        return core

    def test_handshake_error_maps_to_auth(self) -> None:
        core = self._build_core_with_failing_connect(ButtplugHandshakeError("server rejected handshake"))
        with pytest.raises(RewardProviderAuthError, match="handshake refused"):
            core.get_client()

    def test_connector_error_maps_to_offline(self) -> None:
        core = self._build_core_with_failing_connect(ButtplugConnectorError("connection refused"))
        with pytest.raises(RewardDeviceOfflineError, match="unreachable"):
            core.get_client()

    def test_oserror_maps_to_offline(self) -> None:
        core = self._build_core_with_failing_connect(ConnectionRefusedError("nothing listening on 12345"))
        with pytest.raises(RewardDeviceOfflineError, match="unreachable"):
            core.get_client()

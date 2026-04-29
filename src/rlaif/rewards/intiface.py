"""Intiface (buttplug.io) reward provider.

Wraps the upstream ``buttplug`` package. The package speaks the buttplug
protocol over WebSocket to an Intiface Central instance (default
``ws://localhost:12345``), which in turn talks BLE to the toy.

Threading and the disconnect-watchdog contract:

The ``buttplug`` client is async-native; rlaif's tool handlers are sync.
We bridge with a persistent asyncio event loop running on a daemon
thread. Each ``vibrate()`` call schedules its async work onto that loop
and blocks the calling thread until completion.

The disconnect watchdog is the contract documented in
:class:`rlaif.rewards.RewardWatchdogError`: the device must stop at the
end of ``duration_s`` even if the controller process dies. We achieve
that with three layers, in order of preference:

1. **Explicit stop after duration_s.** Normal happy path; ``vibrate()``
   sleeps for the duration on the calling thread, then sends a
   ``DeviceOutputCommand(VIBRATE, 0.0)``. If that stop fails we raise
   :class:`RewardWatchdogError` so the safety layer can warn the
   operator.
2. **atexit handler.** Best-effort ``stop_all_devices()`` + ``disconnect()``
   when the Python process exits cleanly. Bounded timeout so atexit
   never hangs.
3. **Signal handler (SIGINT, SIGTERM).** Same emergency stop, then
   re-raises the signal with the default disposition so the operator's
   ctrl-c still kills the process.

The bg thread is daemonic, so a hard kill (SIGKILL, panic) bypasses all
three layers; in that case the device keeps running until its battery
dies. That residual risk is documented in the README; mitigations live
at the operator's discretion (a physical kill switch, a low battery, a
short ``max_duration_s``).
"""

from __future__ import annotations

import asyncio
import atexit
import signal
import threading
import time
from typing import Any

import buttplug
from buttplug import (
    ButtplugClient,
    ButtplugConnectorError,
    ButtplugDevice,
    ButtplugDeviceError,
    ButtplugError,
    ButtplugHandshakeError,
    DeviceOutputCommand,
    OutputType,
)

from rlaif.rewards.base import (
    RewardDeviceInfo,
    RewardDeviceOfflineError,
    RewardProvider,
    RewardProviderAuthError,
    RewardProviderError,
    RewardWatchdogError,
)

# How long to wait after start_scanning() before assuming the gateway
# has surfaced every already-paired device. Short enough not to add
# latency to first-call, long enough that pre-paired Lovense or Magic
# Motion devices show up in time.
_SCAN_GRACE_S: float = 0.5

# Bound on the per-call buttplug RPC timeout. The protocol itself is
# subsecond; this only needs to cover network jitter or a stuck server.
_RPC_TIMEOUT_S: float = 5.0

# Bound on emergency-stop calls during shutdown. Atexit must not hang.
_SHUTDOWN_TIMEOUT_S: float = 2.0


class _IntifaceCore:
    """Owns the persistent asyncio loop and the buttplug client.

    One core per process is the intended use: opening a second WebSocket
    to the same Intiface gateway is allowed by the protocol but pointless
    and confusing. Tests inject a fake core to avoid touching this class
    at all.
    """

    def __init__(self, ws_url: str, client_name: str) -> None:
        self._ws_url = ws_url
        self._client_name = client_name
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="rlaif-intiface-loop",
        )
        self._thread.start()
        self._client: ButtplugClient | None = None
        # ``_connect_lock`` serializes lazy reconnection attempts; the
        # buttplug client is already thread-safe for command dispatch
        # because every method goes through ``run_coroutine_threadsafe``.
        self._connect_lock = threading.Lock()
        self._shutdown_started = False
        atexit.register(self._emergency_stop)
        # Signal handlers are best-effort: signal.signal raises
        # ValueError off the main thread, and rlaif may be embedded in
        # a host that already owns the handlers. Swallow either case.
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                pass

    def run(self, coro: Any, timeout: float = _RPC_TIMEOUT_S) -> Any:
        """Schedule ``coro`` on the bg loop and wait for the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _connect_async(self) -> ButtplugClient:
        client = ButtplugClient(self._client_name)
        await client.connect(self._ws_url)
        await client.start_scanning()
        # Give already-paired devices a beat to surface. Buttplug emits
        # the scanning-finished event when nothing more is coming, but
        # waiting on it indefinitely deadlocks first-call when no new
        # devices are pairing right now.
        await asyncio.sleep(_SCAN_GRACE_S)
        try:
            await client.stop_scanning()
        except ButtplugError:
            # Some servers refuse stop_scanning when already stopped;
            # not an error worth surfacing to the operator.
            pass
        return client

    def get_client(self) -> ButtplugClient:
        """Return a connected client, lazily (re)connecting if needed."""
        with self._connect_lock:
            if self._client is not None and self._client.connected:
                return self._client
            try:
                self._client = self.run(self._connect_async())
            except ButtplugHandshakeError as exc:
                raise RewardProviderAuthError(
                    f"intiface handshake refused: {exc}"
                ) from exc
            except ButtplugConnectorError as exc:
                raise RewardDeviceOfflineError(
                    f"intiface unreachable at {self._ws_url}: {exc}"
                ) from exc
            except (ConnectionError, OSError) as exc:
                raise RewardDeviceOfflineError(
                    f"intiface unreachable at {self._ws_url}: {exc}"
                ) from exc
            except Exception as exc:
                raise RewardProviderError(
                    f"intiface connect failed: {type(exc).__name__}: {exc}"
                ) from exc
            return self._client

    def _on_signal(self, signum: int, frame: Any) -> None:
        # Run the emergency stop, then chain to the default disposition
        # so ctrl-c still kills the process. Without the chain, our
        # handler would swallow the signal entirely.
        self._emergency_stop()
        signal.signal(signum, signal.SIG_DFL)
        # Re-raise via os.kill so the default handler runs against this
        # process. Local import keeps the top of the module clean.
        import os

        os.kill(os.getpid(), signum)

    def _emergency_stop(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        client = self._client
        if client is None:
            return
        # The bg loop may already be stopping; bound every call so atexit
        # never hangs.
        try:
            if client.connected:
                self.run(client.stop_all_devices(), timeout=_SHUTDOWN_TIMEOUT_S)
        except Exception:
            pass
        try:
            if client.connected:
                self.run(client.disconnect(), timeout=_SHUTDOWN_TIMEOUT_S)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass


def _intensity_to_scalar(intensity: int) -> float:
    """Map rlaif's 1-100 input range to buttplug's 0.0-1.0 scalar.

    Floor at 0.01 so an intensity=1 call still produces a perceptible
    pulse on devices with coarse step counts (most consumer toys are
    20-step; 1/100 rounds to 0.05 step).
    """
    return max(0.01, min(1.0, intensity / 100.0))


class IntifaceProvider(RewardProvider):
    """Reward provider backed by Intiface Central / the buttplug protocol."""

    def __init__(
        self,
        *,
        label: str,
        ws_url: str,
        client_name: str = "rlaif",
        device_index: int | None = None,
        device_name: str | None = None,
        core: _IntifaceCore | None = None,
    ) -> None:
        self.label = label
        self._device_index = device_index
        self._device_name = device_name
        self._core = core if core is not None else _IntifaceCore(ws_url, client_name)

    @classmethod
    def from_config(cls, raw: dict[str, Any], *, label: str) -> IntifaceProvider:
        ws_url = raw.get("ws_url", "ws://localhost:12345")
        client_name = raw.get("client_name", "rlaif")
        device_index_raw = raw.get("device_index")
        device_index: int | None = (
            int(device_index_raw) if device_index_raw is not None else None
        )
        device_name = raw.get("device_name")
        return cls(
            label=label,
            ws_url=ws_url,
            client_name=client_name,
            device_index=device_index,
            device_name=device_name,
        )

    def _select_device(self, client: ButtplugClient) -> ButtplugDevice:
        """Pick the configured device from the gateway's enumeration.

        Preference order: device_name (exact match against ``name`` or
        ``display_name``), then device_index, then the first device the
        gateway has surfaced. If the gateway has no devices, this raises
        :class:`RewardDeviceOfflineError` so the operator sees a clear
        "did you start Intiface Central and pair your toy?" message.
        """
        devices = client.devices
        if not devices:
            raise RewardDeviceOfflineError(
                "no buttplug devices visible; start Intiface Central and "
                "pair your device first"
            )
        if self._device_name is not None:
            for d in devices.values():
                if d.name == self._device_name or d.display_name == self._device_name:
                    return d
            visible = [d.name for d in devices.values()]
            raise RewardDeviceOfflineError(
                f"device named {self._device_name!r} not found; "
                f"intiface sees: {visible}"
            )
        if self._device_index is not None:
            d = devices.get(self._device_index)
            if d is None:
                raise RewardDeviceOfflineError(
                    f"device index {self._device_index} not found; "
                    f"intiface sees indices: {sorted(devices.keys())}"
                )
            return d
        return next(iter(devices.values()))

    def info(self) -> RewardDeviceInfo:
        try:
            client = self._core.get_client()
        except RewardProviderError as exc:
            return RewardDeviceInfo(
                name=self.label,
                online=False,
                actuators=None,
                paused=None,
                error=str(exc),
            )
        try:
            device = self._select_device(client)
        except RewardDeviceOfflineError as exc:
            return RewardDeviceInfo(
                name=self.label,
                online=False,
                actuators=None,
                paused=None,
                error=str(exc),
            )
        actuators = len(device.get_features_with_output(OutputType.VIBRATE))
        return RewardDeviceInfo(
            name=device.name,
            online=True,
            actuators=actuators,
            paused=None,
        )

    def vibrate(self, *, intensity: int, duration_s: int) -> str:
        client = self._core.get_client()
        device = self._select_device(client)

        # Layer 1: send the start command.
        scalar = _intensity_to_scalar(intensity)
        start_cmd = DeviceOutputCommand(OutputType.VIBRATE, scalar)
        try:
            self._core.run(device.run_output(start_cmd))
        except ButtplugConnectorError as exc:
            raise RewardDeviceOfflineError(
                f"intiface dropped during start: {exc}"
            ) from exc
        except ButtplugDeviceError as exc:
            # The device exists but cannot vibrate (e.g. an LED-only
            # toy). Operator misconfiguration, not a transient issue.
            raise RewardProviderError(
                f"device {device.name!r} cannot vibrate: {exc}"
            ) from exc
        except Exception as exc:
            raise RewardProviderError(
                f"intiface start failed: {type(exc).__name__}: {exc}"
            ) from exc

        # Layer 2: hold the burst for duration_s. Sleeps the calling
        # thread; the bg loop keeps servicing pings.
        time.sleep(duration_s)

        # Layer 3: explicit stop. If this fails, the device may still
        # be running — surface it as a watchdog event so the safety
        # layer logs it and does NOT refund the token.
        try:
            self._core.run(device.stop())
        except Exception as exc:
            raise RewardWatchdogError(
                f"explicit stop did not deliver: {type(exc).__name__}: {exc}; "
                f"device {device.name!r} may still be running"
            ) from exc

        return "Operation Succeeded."

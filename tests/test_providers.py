"""Provider-layer tests.

The OpenShock tests use ``httpx.MockTransport`` to stub the HTTP layer —
no real network calls. The PiShock tests use ``MagicMock`` against the
upstream pishock objects.

The mock provider is exercised throughout the rest of the suite already;
nothing extra here for it.
"""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pishock
import pytest

from rlaif.providers import (
    DeviceOfflineError,
    DevicePausedError,
    ProviderAuthError,
    ProviderError,
    ShockNotAllowedError,
    build_provider,
)
from rlaif.providers.mock import MockProvider
from rlaif.providers.openshock import (
    DEFAULT_BASE_URL,
    DURATION_API_MAX_MS,
    INTENSITY_API_MAX,
    OpenShockProvider,
)
from rlaif.providers.pishock import PiShockProvider

# ---------------------------------------------------------------------------
# build_provider dispatch
# ---------------------------------------------------------------------------


def test_build_provider_openshock() -> None:
    p = build_provider(
        "openshock",
        {"api_token": "T", "shocker_id": "abc"},
        label="dev",
    )
    assert isinstance(p, OpenShockProvider)


def test_build_provider_mock() -> None:
    p = build_provider("mock", {}, label="dev")
    assert isinstance(p, MockProvider)


def test_build_provider_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown provider kind"):
        build_provider("bogus", {}, label="dev")


def test_pishock_from_config_missing_field() -> None:
    with pytest.raises(ValueError, match="api_key"):
        PiShockProvider.from_config({"username": "u", "sharecode": "s"}, label="x")


def test_openshock_from_config_missing_field() -> None:
    with pytest.raises(ValueError, match="shocker_id"):
        OpenShockProvider.from_config({"api_token": "T"}, label="x")


# ---------------------------------------------------------------------------
# PiShock provider — error normalization
# ---------------------------------------------------------------------------


def _pishock_provider_with_mocks(
    *, info_side_effect: Any = None, shock_side_effect: Any = None
) -> tuple[PiShockProvider, Any]:
    shocker = MagicMock(spec=pishock.HTTPShocker)
    if info_side_effect is not None:
        shocker.info.side_effect = info_side_effect
    else:
        info = MagicMock()
        info.name = "x"
        info.is_paused = False
        info.max_intensity = 100
        info.max_duration = 15
        shocker.info.return_value = info
    if shock_side_effect is not None:
        shocker.shock.side_effect = shock_side_effect
    else:
        shocker.shock.return_value = None
    api = MagicMock(spec=pishock.PiShockAPI)
    p = PiShockProvider.from_components(api=api, shocker=shocker, label="x")
    return p, shocker


def test_pishock_info_offline_returns_offline_devinfo() -> None:
    p, _ = _pishock_provider_with_mocks(
        info_side_effect=pishock.DeviceNotConnectedError("offline")
    )
    info = p.info()
    assert info.online is False
    assert info.error is None  # DeviceNotConnected is a known offline state, not an error


def test_pishock_info_auth_error_surfaced() -> None:
    p, _ = _pishock_provider_with_mocks(
        info_side_effect=pishock.NotAuthorizedError("bad creds")
    )
    info = p.info()
    assert info.online is False
    assert info.error is not None
    assert "NotAuthorizedError" in info.error


def test_pishock_shock_offline_raises_normalized() -> None:
    p, _ = _pishock_provider_with_mocks(
        shock_side_effect=pishock.DeviceNotConnectedError("offline")
    )
    with pytest.raises(DeviceOfflineError):
        p.shock(intensity=1, duration_s=1)


def test_pishock_shock_paused_raises_normalized() -> None:
    p, _ = _pishock_provider_with_mocks(
        shock_side_effect=pishock.ShockerPausedError("paused")
    )
    with pytest.raises(DevicePausedError):
        p.shock(intensity=1, duration_s=1)


def test_pishock_shock_not_allowed_raises_normalized() -> None:
    p, _ = _pishock_provider_with_mocks(
        shock_side_effect=pishock.ShockNotAllowedError("nope")
    )
    with pytest.raises(ShockNotAllowedError):
        p.shock(intensity=1, duration_s=1)


def test_pishock_shock_auth_error_raises_normalized() -> None:
    p, _ = _pishock_provider_with_mocks(
        shock_side_effect=pishock.NotAuthorizedError("bad creds")
    )
    with pytest.raises(ProviderAuthError):
        p.shock(intensity=1, duration_s=1)


# ---------------------------------------------------------------------------
# OpenShock provider — happy path + every error code we care about
# ---------------------------------------------------------------------------


def _openshock_provider(transport: httpx.MockTransport) -> OpenShockProvider:
    client = httpx.Client(
        base_url=DEFAULT_BASE_URL,
        timeout=2.0,
        transport=transport,
        headers={
            "OpenShockToken": "test-token",
            "User-Agent": "rlaif-test",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    return OpenShockProvider(
        api_token="test-token",
        shocker_id="abc",
        label="test",
        client=client,
    )


def test_openshock_info_online() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"data": {"id": "abc", "name": "left-thigh", "isPaused": False}},
        )

    p = _openshock_provider(httpx.MockTransport(handler))
    info = p.info()
    assert info.online is True
    assert info.name == "left-thigh"
    assert info.paused is False
    assert info.api_max_intensity == INTENSITY_API_MAX
    assert info.api_max_duration_s == DURATION_API_MAX_MS // 1000
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/1/shockers/abc"
    # Confirm auth header was sent.
    assert seen[0].headers["OpenShockToken"] == "test-token"


def test_openshock_info_paused_field_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"id": "abc", "name": "x", "isPaused": True}},
        )

    p = _openshock_provider(httpx.MockTransport(handler))
    info = p.info()
    assert info.paused is True


def test_openshock_info_unauthorized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    p = _openshock_provider(httpx.MockTransport(handler))
    info = p.info()
    assert info.online is False
    assert info.error is not None
    assert "auth" in info.error
    assert "401" in info.error


def test_openshock_info_404_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    p = _openshock_provider(httpx.MockTransport(handler))
    info = p.info()
    assert info.online is False
    assert info.error is not None
    assert "shocker_id not found" in info.error


def test_openshock_info_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail")

    p = _openshock_provider(httpx.MockTransport(handler))
    info = p.info()
    assert info.online is False
    assert info.error is not None
    assert "network" in info.error


def test_openshock_shock_happy_path_sends_correct_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"message": "ok"})

    p = _openshock_provider(httpx.MockTransport(handler))
    resp = p.shock(intensity=20, duration_s=2)
    assert resp == "Operation Succeeded."
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/2/shockers/control"
    body: dict[str, Any] = json.loads(seen[0].content)
    assert "shocks" in body
    one = body["shocks"][0]
    assert one["id"] == "abc"
    assert one["type"] == "Shock"
    assert one["intensity"] == 20
    assert one["duration"] == 2000  # 2s -> 2000ms
    assert one["exclusive"] is True
    assert body["customName"] == "rlaif"


def test_openshock_shock_minimum_duration_floor() -> None:
    # Even though the safety layer never sends < 1s, make sure the
    # provider's floor of 300ms holds on contrived input.
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    p = _openshock_provider(httpx.MockTransport(handler))
    p.shock(intensity=1, duration_s=0)
    body: dict[str, Any] = json.loads(seen[0].content)
    assert body["shocks"][0]["duration"] == 300


def test_openshock_shock_412_paused_raises_paused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, json={"detail": "Shocker paused"})

    p = _openshock_provider(httpx.MockTransport(handler))
    with pytest.raises(DevicePausedError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_404_raises_offline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "ShockerNotFound"})

    p = _openshock_provider(httpx.MockTransport(handler))
    with pytest.raises(DeviceOfflineError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_403_raises_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "no permission"})

    p = _openshock_provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderAuthError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_400_raises_not_allowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "intensity out of range"})

    p = _openshock_provider(httpx.MockTransport(handler))
    with pytest.raises(ShockNotAllowedError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_500_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    p = _openshock_provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_timeout_raises_offline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("read timeout")

    p = _openshock_provider(httpx.MockTransport(handler))
    with pytest.raises(DeviceOfflineError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_network_error_raises_offline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    p = _openshock_provider(httpx.MockTransport(handler))
    with pytest.raises(DeviceOfflineError):
        p.shock(intensity=1, duration_s=1)

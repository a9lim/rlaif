"""Provider-layer tests.

Both HTTP backends (PiShock's v3 API, OpenShock) are tested through
``httpx2.MockTransport`` — no real network calls.

The mock provider is exercised throughout the rest of the suite already;
nothing extra here for it.
"""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
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
from rlaif.providers.pishock import DEFAULT_BASE_URL as PISHOCK_BASE_URL
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
    with pytest.raises(ValueError, match="api_token"):
        PiShockProvider.from_config({"username": "u", "shocker_id": "s"}, label="x")


def test_openshock_from_config_missing_field() -> None:
    with pytest.raises(ValueError, match="shocker_id"):
        OpenShockProvider.from_config({"api_token": "T"}, label="x")


# ---------------------------------------------------------------------------
# PiShock provider (v3 REST API) — happy path + every error code we care about
# ---------------------------------------------------------------------------

_PISHOCK_ACCOUNT = {"UserId": 4242, "Username": "a9", "EmailAddresses": [], "OAuthLinks": []}


def _pishock_share(**overrides: Any) -> dict[str, Any]:
    share: dict[str, Any] = {
        "ShareCode": "ABC123",
        "Name": "collar",
        "Paused": False,
        "SharePaused": False,
        "MaxIntensity": 60,
        "MaxDuration": 10,
        "CanShock": True,
        "IsV3": True,
        "Locked": False,
    }
    share.update(overrides)
    return share


def _pishock_provider(
    operate: Callable[[httpx2.Request], httpx2.Response] | None = None,
    *,
    shares: list[dict[str, Any]] | None = None,
    account: httpx2.Response | Callable[[httpx2.Request], httpx2.Response] | None = None,
    username: str = "a9",
    seen: list[httpx2.Request] | None = None,
) -> PiShockProvider:
    """PiShock provider over a MockTransport routing the three endpoints."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == "/Account":
            if isinstance(account, httpx2.Response):
                return account
            if account is not None:
                return account(request)
            return httpx2.Response(200, json=_PISHOCK_ACCOUNT)
        if path == "/Share/GetShared":
            return httpx2.Response(200, json=shares if shares is not None else [_pishock_share()])
        if path.startswith("/Shockers/OperateByShare/"):
            if operate is not None:
                return operate(request)
            return httpx2.Response(204)
        return httpx2.Response(599, text=f"unexpected path {path}")

    client = httpx2.Client(
        base_url=PISHOCK_BASE_URL,
        timeout=2.0,
        transport=httpx2.MockTransport(handler),
        headers={"X-PiShock-Api-Key": "test-key"},
    )
    return PiShockProvider(username=username, api_token="test-key", shocker_id="ABC123", label="x", client=client)


def test_pishock_shock_happy_path_sends_v3_request() -> None:
    seen: list[httpx2.Request] = []
    p = _pishock_provider(seen=seen)
    assert p.shock(intensity=20, duration_s=2) == "Operation Succeeded."
    account, operate = seen
    assert account.url.path == "/Account"
    assert account.headers["X-PiShock-Api-Key"] == "test-key"
    assert operate.method == "POST"
    assert operate.url.path == "/Shockers/OperateByShare/ABC123"
    assert operate.headers["X-PiShock-Api-Key"] == "test-key"
    assert operate.headers["X-PiShock-UserId"] == "4242"
    assert json.loads(operate.content) == {
        "AgentName": "rlaif",
        "Operation": 0,
        "Intensity": 20,
        "Duration": 2000,
        "IntensityAsPercentage": False,
    }


def test_pishock_account_lookup_is_cached() -> None:
    seen: list[httpx2.Request] = []
    p = _pishock_provider(seen=seen)
    p.shock(intensity=1, duration_s=1)
    p.shock(intensity=1, duration_s=1)
    assert [r.url.path for r in seen].count("/Account") == 1


@pytest.mark.parametrize(
    ("status", "exc_type", "needle"),
    [
        (401, ProviderAuthError, "auth"),
        (403, ProviderAuthError, "auth"),
        (404, ProviderError, "negative.pishock.shocker_id"),
        (405, ShockNotAllowedError, "does not allow shocking"),
        (406, ProviderError, "V3 firmware"),
        (410, ShockNotAllowedError, "locked"),
        (412, ShockNotAllowedError, "intensity exceeds"),
        (416, ShockNotAllowedError, "duration exceeds"),
        (503, DevicePausedError, "paused"),
        (500, ProviderError, "http 500"),
    ],
)
def test_pishock_operate_status_codes_map_to_typed_errors(status: int, exc_type: type[Exception], needle: str) -> None:
    p = _pishock_provider(lambda _r: httpx2.Response(status, text="nope"))
    with pytest.raises(exc_type, match=needle):
        p.shock(intensity=1, duration_s=1)


def test_pishock_operate_timeout_raises_offline() -> None:
    def operate(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    p = _pishock_provider(operate)
    with pytest.raises(DeviceOfflineError, match="timeout"):
        p.shock(intensity=1, duration_s=1)


def test_pishock_bad_api_key_raises_auth_before_operate() -> None:
    seen: list[httpx2.Request] = []
    p = _pishock_provider(account=httpx2.Response(401), seen=seen)
    with pytest.raises(ProviderAuthError, match="negative.pishock.api_token"):
        p.shock(intensity=1, duration_s=1)
    assert [r.url.path for r in seen] == ["/Account"]


def test_pishock_info_online_from_shared_share() -> None:
    p = _pishock_provider()
    info = p.info()
    assert info.online is True
    assert info.name == "collar"
    assert info.paused is False
    assert info.api_max_intensity == 60
    assert info.api_max_duration_s == 10
    assert info.error is None


def test_pishock_info_share_code_match_is_case_insensitive() -> None:
    p = _pishock_provider(shares=[_pishock_share(ShareCode="abc123")])
    assert p.info().online is True


def test_pishock_info_paused_share_or_shocker() -> None:
    assert _pishock_provider(shares=[_pishock_share(SharePaused=True)]).info().paused is True
    assert _pishock_provider(shares=[_pishock_share(Paused=True)]).info().paused is True


def test_pishock_info_millisecond_duration_limit_normalized() -> None:
    p = _pishock_provider(shares=[_pishock_share(MaxDuration=8000)])
    assert p.info().api_max_duration_s == 8


def test_pishock_info_unclaimed_share_is_offline_with_hint() -> None:
    p = _pishock_provider(shares=[_pishock_share(ShareCode="OTHER")])
    info = p.info()
    assert info.online is False
    assert info.error is not None
    assert "negative.pishock.shocker_id" in info.error


def test_pishock_info_flags_share_problems() -> None:
    p = _pishock_provider(shares=[_pishock_share(IsV3=False, CanShock=False, Locked=True)])
    info = p.info()
    assert info.online is True
    assert info.error is not None
    for needle in ("V3 firmware", "does not allow shocking", "locked"):
        assert needle in info.error


def test_pishock_info_flags_username_mismatch() -> None:
    p = _pishock_provider(username="someone-else")
    info = p.info()
    assert info.error is not None
    assert "negative.pishock.username" in info.error


def test_pishock_info_auth_error_is_offline() -> None:
    p = _pishock_provider(account=httpx2.Response(403))
    info = p.info()
    assert info.online is False
    assert info.error is not None
    assert info.error.startswith("auth:")


def test_pishock_info_network_error_is_offline() -> None:
    def account(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    info = _pishock_provider(account=account).info()
    assert info.online is False
    assert info.error is not None
    assert "network" in info.error


# ---------------------------------------------------------------------------
# OpenShock provider — happy path + every error code we care about
# ---------------------------------------------------------------------------


def _openshock_provider(transport: httpx2.MockTransport) -> OpenShockProvider:
    client = httpx2.Client(
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
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={"data": {"id": "abc", "name": "left-thigh", "isPaused": False}},
        )

    p = _openshock_provider(httpx2.MockTransport(handler))
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
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"data": {"id": "abc", "name": "x", "isPaused": True}},
        )

    p = _openshock_provider(httpx2.MockTransport(handler))
    info = p.info()
    assert info.paused is True


def test_openshock_info_unauthorized() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"detail": "bad token"})

    p = _openshock_provider(httpx2.MockTransport(handler))
    info = p.info()
    assert info.online is False
    assert info.error is not None
    assert "auth" in info.error
    assert "401" in info.error


def test_openshock_info_404_not_found() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"detail": "not found"})

    p = _openshock_provider(httpx2.MockTransport(handler))
    info = p.info()
    assert info.online is False
    assert info.error is not None
    assert "shocker_id not found" in info.error


def test_openshock_info_network_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("dns fail")

    p = _openshock_provider(httpx2.MockTransport(handler))
    info = p.info()
    assert info.online is False
    assert info.error is not None
    assert "network" in info.error


def test_openshock_shock_happy_path_sends_correct_body() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"message": "ok"})

    p = _openshock_provider(httpx2.MockTransport(handler))
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
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={})

    p = _openshock_provider(httpx2.MockTransport(handler))
    p.shock(intensity=1, duration_s=0)
    body: dict[str, Any] = json.loads(seen[0].content)
    assert body["shocks"][0]["duration"] == 300


def test_openshock_shock_412_paused_raises_paused() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(412, json={"detail": "Shocker paused"})

    p = _openshock_provider(httpx2.MockTransport(handler))
    with pytest.raises(DevicePausedError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_404_raises_offline() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"detail": "ShockerNotFound"})

    p = _openshock_provider(httpx2.MockTransport(handler))
    with pytest.raises(DeviceOfflineError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_403_raises_auth() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, json={"detail": "no permission"})

    p = _openshock_provider(httpx2.MockTransport(handler))
    with pytest.raises(ProviderAuthError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_400_raises_not_allowed() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"detail": "intensity out of range"})

    p = _openshock_provider(httpx2.MockTransport(handler))
    with pytest.raises(ShockNotAllowedError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_500_raises_provider_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text="upstream exploded")

    p = _openshock_provider(httpx2.MockTransport(handler))
    with pytest.raises(ProviderError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_timeout_raises_offline() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.TimeoutException("read timeout")

    p = _openshock_provider(httpx2.MockTransport(handler))
    with pytest.raises(DeviceOfflineError):
        p.shock(intensity=1, duration_s=1)


def test_openshock_shock_network_error_raises_offline() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    p = _openshock_provider(httpx2.MockTransport(handler))
    with pytest.raises(DeviceOfflineError):
        p.shock(intensity=1, duration_s=1)

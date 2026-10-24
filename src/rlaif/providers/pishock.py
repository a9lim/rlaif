"""PiShock backend (https://pishock.com), over the v3 REST API.

Talks to ``https://api.pishock.com`` directly with httpx2. The legacy
``do.pishock.com`` API — and the ``pishock`` SDK that wrapped it — has
returned 404 since about March 2026, so there is no SDK in the loop.

Auth: the per-account ``api_token`` (what pishock.com calls the "API key")
in the ``X-PiShock-Api-Key`` header. ``GET /Account`` resolves the key's
numeric user id, which rides along as ``X-PiShock-UserId``, and its
username, which ``info()`` checks against the configured ``username`` so a
key pasted from the wrong account shows up in ``rlaif doctor``.

Device: ``shocker_id`` is the share code (pishock.com's "share code"); the
field names match OpenShock's so both backends share one vocabulary.

Endpoints used:

* ``GET  /Account``                            — user id + username for the key
* ``GET  /Share/GetShared``                     — the share's name, pause state, limits
* ``POST /Shockers/OperateByShare/{ShareCode}`` — fire; ``204`` on success

Operate status codes (from the API's swagger, fetched 2026-10-24):
``401``/``403`` auth, ``404`` unknown share, ``405`` operation not allowed on
the share, ``406`` hub not on V3 firmware, ``410`` share locked, ``412``
intensity over the share's limit, ``416`` duration over the share's limit,
``503`` share or shocker paused.

Capability ranges the API enforces: intensity 0–100, duration 16–15000 ms.
The safety layer's caps are typically much tighter than these.
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import quote

import httpx2

from rlaif.providers.base import (
    DeviceInfo,
    DeviceOfflineError,
    DevicePausedError,
    Provider,
    ProviderAuthError,
    ProviderError,
    ShockNotAllowedError,
)

DEFAULT_BASE_URL: str = "https://api.pishock.com"

# Upstream API ceilings — informational only, surfaced via ``DeviceInfo``.
PISHOCK_API_MAX_INTENSITY: int = 100
PISHOCK_API_MAX_DURATION_S: int = 15

# ``ShockerOperation`` enum in the v3 API: 0 shock, 1 vibrate, 2 beep.
_OPERATION_SHOCK: int = 0

# The Intiface and OpenShock backends both reach the gateway as "rlaif"; do
# the same here so log lines on every provider's dashboard agree on who
# fired the call. (The API notes ``AgentName`` will eventually give way to
# the API key's own name.)
_AGENT_NAME: str = "rlaif"
_USER_AGENT: str = "rlaif/pishock-provider"


class PiShockProvider(Provider):
    """PiShock v3 REST API provider."""

    def __init__(
        self,
        *,
        username: str,
        api_token: str,
        shocker_id: str,
        label: str = "pishock",
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 5.0,
        client: httpx2.Client | None = None,
    ) -> None:
        self.label = label
        self._username = username
        self._shocker_id = shocker_id
        if client is not None:
            # Test injection — the caller has already pre-configured headers.
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx2.Client(
                base_url=base_url.rstrip("/"),
                timeout=timeout_s,
                headers={
                    "X-PiShock-Api-Key": api_token,
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/json",
                },
            )
            self._owns_client = True
        self._user_id: int | None = None
        self._account_username: str | None = None

    def __del__(self) -> None:  # pragma: no cover - cleanup
        try:
            if getattr(self, "_owns_client", False):
                self._client.close()
        except Exception:
            pass

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

    # --------------------------------------------------------------- account

    def _account(self) -> int:
        """Resolve (once) the numeric user id behind the API key."""
        if self._user_id is not None:
            return self._user_id
        try:
            r = self._client.get("/Account")
        except httpx2.RequestError as exc:
            raise DeviceOfflineError(f"network: {type(exc).__name__}: {exc}") from exc
        if r.status_code in (401, 403):
            raise ProviderAuthError(
                f"api.pishock.com rejected the API key (http {r.status_code}); check negative.pishock.api_token"
            )
        if not r.is_success:
            raise ProviderError(f"GET /Account: http {r.status_code}: {_truncate(r.text)}")
        try:
            data = cast(dict[str, Any], r.json())
            user_id = int(data["UserId"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError(f"GET /Account: unexpected response: {_truncate(r.text)}") from exc
        username_any: Any = data.get("Username")
        self._account_username = str(username_any) if username_any is not None else None
        self._user_id = user_id
        return user_id

    def _user_headers(self, user_id: int) -> dict[str, str]:
        return {"X-PiShock-UserId": str(user_id)}

    # ------------------------------------------------------------------ info

    def _offline(self, error: str) -> DeviceInfo:
        return DeviceInfo(
            name=self.label,
            online=False,
            paused=None,
            api_max_intensity=None,
            api_max_duration_s=None,
            error=error,
        )

    def info(self) -> DeviceInfo:
        try:
            user_id = self._account()
        except ProviderAuthError as exc:
            return self._offline(f"auth: {exc}")
        except ProviderError as exc:
            return self._offline(str(exc))

        try:
            r = self._client.get("/Share/GetShared", headers=self._user_headers(user_id))
        except httpx2.RequestError as exc:
            return self._offline(f"network: {type(exc).__name__}: {exc}")
        if r.status_code in (401, 403):
            return self._offline(f"auth: http {r.status_code}")
        if not r.is_success:
            return self._offline(f"http {r.status_code}: {_truncate(r.text)}")
        try:
            shares_any: Any = r.json()
        except ValueError as exc:
            return self._offline(f"invalid json: {exc}")
        shares: list[Any] = cast(list[Any], shares_any) if isinstance(shares_any, list) else []

        wanted = self._shocker_id.strip().casefold()
        share: dict[str, Any] | None = None
        for entry in shares:
            if isinstance(entry, dict):
                entry_d = cast(dict[str, Any], entry)
                if str(entry_d.get("ShareCode", "")).strip().casefold() == wanted:
                    share = entry_d
                    break
        if share is None:
            return self._offline(
                "share code not among the shares this API key's account has claimed "
                "(GET /Share/GetShared) — claim it on pishock.com or check negative.pishock.shocker_id"
            )

        problems: list[str] = []
        if (
            self._account_username is not None
            and self._account_username.casefold() != self._username.strip().casefold()
        ):
            problems.append(
                f"api_token belongs to PiShock user {self._account_username!r}, "
                f"not negative.pishock.username {self._username!r}"
            )
        if share.get("IsV3") is False:
            problems.append("hub is not on V3 firmware; PiShock's API refuses non-V3 devices")
        if share.get("CanShock") is False:
            problems.append("share does not allow shocking")
        if share.get("Locked"):
            problems.append("share is locked")

        name_any: Any = share.get("Name")
        return DeviceInfo(
            name=str(name_any) if name_any else self.label,
            online=True,
            paused=bool(share.get("Paused")) or bool(share.get("SharePaused")),
            api_max_intensity=_int_or_none(share.get("MaxIntensity")),
            api_max_duration_s=_duration_s(share.get("MaxDuration")),
            error="; ".join(problems) or None,
        )

    # ----------------------------------------------------------------- shock

    def shock(self, *, intensity: int, duration_s: int) -> str:
        user_id = self._account()
        body = {
            "AgentName": _AGENT_NAME,
            "Operation": _OPERATION_SHOCK,
            "Intensity": intensity,
            "Duration": duration_s * 1000,
            "IntensityAsPercentage": False,
        }
        try:
            r = self._client.post(
                f"/Shockers/OperateByShare/{quote(self._shocker_id, safe='')}",
                json=body,
                headers=self._user_headers(user_id),
            )
        except httpx2.TimeoutException as exc:
            raise DeviceOfflineError(f"timeout: {exc}") from exc
        except httpx2.RequestError as exc:
            raise DeviceOfflineError(f"network: {type(exc).__name__}: {exc}") from exc

        status = r.status_code
        if status in (401, 403):
            raise ProviderAuthError(f"auth: http {status}: {_truncate(r.text)}")
        if status == 404:
            raise ProviderError(f"share not found (check negative.pishock.shocker_id): {_truncate(r.text)}")
        if status == 405:
            raise ShockNotAllowedError(f"share does not allow shocking: {_truncate(r.text)}")
        if status == 406:
            raise ProviderError(
                "hub is not on V3 firmware; update it from pishock.com before PiShock's API will drive it"
            )
        if status == 410:
            raise ShockNotAllowedError(f"share is locked: {_truncate(r.text)}")
        if status in (412, 416):
            # Our caps already clamped the request; the share's own limits
            # are tighter.
            what = "intensity" if status == 412 else "duration"
            raise ShockNotAllowedError(f"{what} exceeds the share's limit: {_truncate(r.text)}")
        if status == 503:
            raise DevicePausedError(f"share or shocker paused: {_truncate(r.text)}")
        if not r.is_success:
            raise ProviderError(f"http {status}: {_truncate(r.text)}")
        # The API answers 204 with no body; synthesize the same response
        # string every provider returns.
        return "Operation Succeeded."


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _duration_s(value: Any) -> int | None:
    """Share duration limit in whole seconds.

    The v3 schema doesn't state the unit of ``MaxDuration``; share limits
    are set in seconds on the site while operate takes milliseconds, so a
    value past the 15 s API ceiling is read as milliseconds.
    """
    raw = _int_or_none(value)
    if raw is None:
        return None
    return raw if raw <= PISHOCK_API_MAX_DURATION_S else raw // 1000


def _truncate(text: str, *, limit: int = 200) -> str:
    """Bound error-message length so refusal records stay grep-friendly."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"

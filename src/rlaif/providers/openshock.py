"""OpenShock backend (https://openshock.org).

Wraps the OpenShock REST API. Works against:

* the public cloud at ``https://api.openshock.app`` (default)
* any self-hosted OpenShock backend (set ``base_url``)

Auth is a single API token sent in the ``OpenShockToken`` header (generate
one at https://openshock.app/#/dashboard/tokens or your self-hosted
equivalent).

Endpoints used:

* ``GET  /1/shockers/{shocker_id}`` — shocker info
* ``POST /2/shockers/control``     — trigger one or more shocks

Schema sources:
* ``Common/Models/WebSocket/User/Control.cs`` (request body element)
* ``Common/Models/ControlRequest.cs`` (request body)
* ``Common/Constants/HardLimits.cs`` (intensity 0–100 byte, duration
  300–65535 ms)

Capability mapping into rlaif's ``DeviceInfo``:
* ``api_max_intensity`` = 100
* ``api_max_duration_s`` = 65 (rounded down from 65535 ms)
* ``online`` = True iff the shocker GET succeeded. OpenShock does not
  expose per-shocker hub-online state via simple HTTP; the real
  reachability check is firing a control and watching for a
  :class:`DeviceOfflineError`. README documents this.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from rlaif.providers.base import (
    DeviceInfo,
    DeviceOfflineError,
    DevicePausedError,
    Provider,
    ProviderAuthError,
    ProviderError,
    ShockNotAllowedError,
)

DEFAULT_BASE_URL: str = "https://api.openshock.app"
INTENSITY_API_MAX: int = 100
DURATION_API_MAX_MS: int = 65535
DURATION_API_MIN_MS: int = 300

# OpenShock's public API rejects requests with empty User-Agent (returns 403).
# Any non-empty value works; we send our own so log lines on the backend show
# "rlaif" as the source.
_USER_AGENT: str = "rlaif/openshock-provider"


class OpenShockProvider(Provider):
    """OpenShock REST API provider."""

    def __init__(
        self,
        *,
        api_token: str,
        shocker_id: str,
        base_url: str = DEFAULT_BASE_URL,
        label: str = "openshock",
        timeout_s: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.label = label
        self._token = api_token
        self._shocker_id = shocker_id
        self._base_url = base_url.rstrip("/")
        if client is not None:
            # Test injection — the caller has already pre-configured headers.
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                base_url=self._base_url,
                timeout=timeout_s,
                headers={
                    "OpenShockToken": api_token,
                    "User-Agent": _USER_AGENT,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            self._owns_client = True

    def __del__(self) -> None:  # pragma: no cover - cleanup
        try:
            if getattr(self, "_owns_client", False):
                self._client.close()
        except Exception:
            pass

    @classmethod
    def from_config(cls, raw: dict[str, str], *, label: str) -> OpenShockProvider:
        try:
            return cls(
                api_token=raw["api_token"],
                shocker_id=raw["shocker_id"],
                base_url=raw.get("base_url", DEFAULT_BASE_URL),
                label=label,
            )
        except KeyError as exc:
            raise ValueError(f"openshock provider missing required field: {exc.args[0]}") from exc

    # ------------------------------------------------------------------ info

    def info(self) -> DeviceInfo:
        try:
            r = self._client.get(f"/1/shockers/{self._shocker_id}")
        except httpx.RequestError as exc:
            return DeviceInfo(
                name=self.label,
                online=False,
                paused=None,
                api_max_intensity=None,
                api_max_duration_s=None,
                error=f"network: {type(exc).__name__}: {exc}",
            )

        if r.status_code in (401, 403):
            return DeviceInfo(
                name=self.label,
                online=False,
                paused=None,
                api_max_intensity=None,
                api_max_duration_s=None,
                error=f"auth: http {r.status_code}",
            )
        if r.status_code == 404:
            return DeviceInfo(
                name=self.label,
                online=False,
                paused=None,
                api_max_intensity=None,
                api_max_duration_s=None,
                error="shocker_id not found",
            )
        if not r.is_success:
            return DeviceInfo(
                name=self.label,
                online=False,
                paused=None,
                api_max_intensity=None,
                api_max_duration_s=None,
                error=f"http {r.status_code}: {_truncate(r.text)}",
            )

        try:
            payload = cast(dict[str, Any], r.json())
        except ValueError as exc:
            return DeviceInfo(
                name=self.label,
                online=False,
                paused=None,
                api_max_intensity=None,
                api_max_duration_s=None,
                error=f"invalid json: {exc}",
            )
        # OpenShock wraps responses in {data: ...}. Tolerate a flat shape too
        # in case of self-hosted forks.
        data: dict[str, Any] = (
            cast(dict[str, Any], payload["data"]) if isinstance(payload.get("data"), dict) else payload
        )
        name_any: Any = data.get("name", self.label)
        paused_any: Any = data.get("isPaused")
        return DeviceInfo(
            name=str(name_any) if name_any is not None else self.label,
            online=True,
            paused=bool(paused_any) if paused_any is not None else None,
            api_max_intensity=INTENSITY_API_MAX,
            api_max_duration_s=DURATION_API_MAX_MS // 1000,
        )

    # ----------------------------------------------------------------- shock

    def shock(self, *, intensity: int, duration_s: int) -> str:
        duration_ms = max(DURATION_API_MIN_MS, min(DURATION_API_MAX_MS, duration_s * 1000))
        body = {
            "shocks": [
                {
                    "id": self._shocker_id,
                    "type": "Shock",
                    "intensity": intensity,
                    "duration": duration_ms,
                    "exclusive": True,
                }
            ],
            "customName": "rlaif",
        }
        try:
            r = self._client.post("/2/shockers/control", json=body)
        except httpx.TimeoutException as exc:
            raise DeviceOfflineError(f"timeout: {exc}") from exc
        except httpx.RequestError as exc:
            raise DeviceOfflineError(f"network: {type(exc).__name__}: {exc}") from exc

        if r.status_code in (401, 403):
            raise ProviderAuthError(f"auth: http {r.status_code}: {_truncate(r.text)}")
        if r.status_code == 404:
            # /2/shockers/control returns 404 when the shocker id is unknown
            # or not shared with the token; surface as offline so the safety
            # layer refunds the token.
            raise DeviceOfflineError(f"shocker not found: {_truncate(r.text)}")
        if r.status_code == 412:
            # 412 PreconditionFailed = shocker is paused on the OpenShock side.
            raise DevicePausedError(f"shocker paused: {_truncate(r.text)}")
        if r.status_code in (400, 422):
            # Validation error — backend overrode our caps. We've already
            # clamped to the safety layer's caps, so this is the backend
            # tightening further; treat as not allowed.
            raise ShockNotAllowedError(f"http {r.status_code}: {_truncate(r.text)}")
        if not r.is_success:
            raise ProviderError(f"http {r.status_code}: {_truncate(r.text)}")

        return "Operation Succeeded."


def _truncate(text: str, *, limit: int = 200) -> str:
    """Bound error-message length so refusal records stay grep-friendly."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"

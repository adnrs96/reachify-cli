"""Thin backend API client.

Wraps httpx, injects auth headers from the local profile, and returns parsed
JSON. Endpoint-specific shaping lives in the callers (e.g. :mod:`reachify.jobs`).
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import DEFAULT_API_BASE_URL
from .models import Profile


class ApiError(Exception):
    """Raised on transport errors or non-2xx backend responses.

    ``status_code`` is set for HTTP-level failures so callers can branch on it
    (e.g. a 409 from claim means another worker won the race).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ReachifyClient:
    """Authenticated client for the Reachify backend."""

    def __init__(self, profile: Profile, *, timeout: float = 30.0) -> None:
        self._profile = profile
        base_url = profile.api_base_url or DEFAULT_API_BASE_URL
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers=self._auth_headers(profile),
        )

    @staticmethod
    def _auth_headers(profile: Profile) -> dict[str, str]:
        # The Velorify API authenticates judgement-job workers via the
        # ``richefy-api-at`` access-token header. Bearer is kept for the
        # auth-protected endpoints that use it.
        return {
            "richefy-api-at": profile.identity_token,
            "Authorization": f"Bearer {profile.identity_token}",
            "X-Reachify-Id": profile.id,
            "Accept": "application/json",
        }

    def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make a request and return the parsed JSON body (dict or list)."""
        try:
            resp = self._client.request(method, path, **kwargs)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ApiError(
                f"{method} {path} -> {exc.response.status_code}: {exc.response.text}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiError(f"{method} {path} failed: {exc}") from exc

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError(f"{method} {path} returned non-JSON body.") from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ReachifyClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

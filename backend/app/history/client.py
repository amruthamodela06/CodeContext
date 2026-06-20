"""Thin async wrapper around GitHub's GraphQL endpoint.

Responsibilities:
- Auth via Bearer token.
- Surface `rateLimit { cost limit remaining resetAt }` from each response so
  the orchestrator can pause before it hits the wall.
- Pause-and-resume when `remaining` drops below a configurable threshold
  (default 50 points — leaves headroom for one more page in flight).
- Retry transient failures (HTTP 5xx, connection errors) with exponential
  backoff; surface a 401 / unauthorized cleanly so the orchestrator can mark
  the job failed rather than spinning.

The wait can be long (up to an hour if quota is exhausted), so it's done as
`asyncio.sleep` inside the call — the orchestrator stays a simple linear loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

_GITHUB_GRAPHQL = "https://api.github.com/graphql"
# Stop *before* the wall so a query mid-flight (cost ~5-15 points) doesn't
# bounce off it. Tunable via constructor for tests.
_DEFAULT_LOW_WATER_MARK = 50
# Hard cap so a misconfigured resetAt can't make us sleep forever. GitHub's
# actual reset window is at most ~1 hour.
_MAX_PAUSE_SECONDS = 3600


class GitHubAuthError(RuntimeError):
    """Token missing / invalid (401 / 403 without rate-limit indicator)."""


class GitHubGraphQLError(RuntimeError):
    """GraphQL returned an `errors` block. Carries the raw payload."""

    def __init__(self, message: str, errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class GitHubGraphQLClient:
    def __init__(
        self,
        token: str,
        *,
        endpoint: str = _GITHUB_GRAPHQL,
        low_water_mark: int = _DEFAULT_LOW_WATER_MARK,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise GitHubAuthError(
                "GITHUB_TOKEN is empty; required for /ingest-history. "
                "Public-repo-scope token from https://github.com/settings/tokens."
            )
        self._token = token
        self._endpoint = endpoint
        self._low_water_mark = low_water_mark
        # Caller may inject a client (tests do); otherwise we own one. The
        # owned client is created lazily so constructing the wrapper doesn't
        # open a connection pool we might never use.
        self._http: httpx.AsyncClient | None = http_client
        self._owns_http = http_client is None

    async def __aenter__(self) -> GitHubGraphQLClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def execute(self, query: str, variables: dict) -> dict:
        """Run one GraphQL request. Returns the `data` block.

        Pauses internally if `rateLimit.remaining` from the previous response
        sat below the low-water-mark — the orchestrator stays a flat loop.
        Retries 5xx + transient network errors with exponential backoff.
        Raises GitHubAuthError on 401 / unauthorized; GitHubGraphQLError on
        any `errors` block.
        """
        assert self._http is not None, "use as `async with GitHubGraphQLClient(...) as c:`"

        backoff = 1.0
        for attempt in range(4):
            try:
                response = await self._http.post(
                    self._endpoint,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "codecontext",
                    },
                    json={"query": query, "variables": variables},
                )
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                if attempt == 3:
                    raise
                log.warning("GraphQL transient error (%s); backoff %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            if response.status_code == 401:
                raise GitHubAuthError("GitHub returned 401. Token is invalid or expired.")
            if response.status_code == 403:
                # Could be rate-limit (when message mentions it) or missing scope.
                body_text = response.text
                if "rate limit" in body_text.lower():
                    await self._pause_until_reset_header(response)
                    continue
                raise GitHubAuthError(f"GitHub 403: {body_text[:200]}")
            if response.status_code >= 500:
                if attempt == 3:
                    response.raise_for_status()
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                raise GitHubGraphQLError(
                    f"GraphQL errors: {payload['errors'][:2]}",
                    errors=payload["errors"],
                )

            data = payload.get("data") or {}
            await self._maybe_pause_for_rate_limit(data)
            return data

        # Loop fell through without returning — shouldn't happen.
        raise RuntimeError("GraphQL request exhausted retries")

    async def _maybe_pause_for_rate_limit(self, data: dict) -> None:
        rl = data.get("rateLimit")
        if not rl:
            return
        remaining = rl.get("remaining")
        reset_at = rl.get("resetAt")
        if remaining is None or remaining > self._low_water_mark:
            return
        if not reset_at:
            return
        pause = self._seconds_until(reset_at)
        if pause <= 0:
            return
        pause = min(pause, _MAX_PAUSE_SECONDS)
        log.warning(
            "GitHub GraphQL rate-limit near exhaustion (%s remaining); sleeping %.0fs until reset",
            remaining,
            pause,
        )
        await asyncio.sleep(pause)

    async def _pause_until_reset_header(self, response: httpx.Response) -> None:
        # On 403/rate-limit GitHub sends `x-ratelimit-reset` (epoch seconds).
        # Fall back to a 60s pause if the header is missing.
        header = response.headers.get("x-ratelimit-reset")
        if header:
            try:
                reset_epoch = int(header)
                now = int(datetime.now(UTC).timestamp())
                pause = max(1, reset_epoch - now)
            except ValueError:
                pause = 60
        else:
            pause = 60
        pause = min(pause, _MAX_PAUSE_SECONDS)
        log.warning("GitHub returned 403 rate-limit; sleeping %.0fs", pause)
        await asyncio.sleep(pause)

    @staticmethod
    def _seconds_until(iso_ts: str) -> float:
        try:
            reset_at = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        delta = reset_at - datetime.now(UTC)
        return delta.total_seconds()

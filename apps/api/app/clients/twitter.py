"""Async X API v2 clients for shillscore.

Two clients, two auth modes:
- `TwitterClient`: user-context OAuth 2.0 (connected user's access token).
  Used for /users/me + /users/:id/following.
- `AppOnlyTwitterClient`: app-only Bearer (client_credentials grant).
  Required for /tweets/search/all and /tweets/counts/all — those endpoints
  reject user tokens.

Pay-per-use means every returned post costs credit. Design contract:
- Server-side filter (has:cashtags -is:retweet) keeps cost ~10–15% of the
  raw timeline volume.
- Engagement floor is applied by the worker on returned `like_count`
  because v2 has no min_faves operator.
- Caller manages rate-of-call discipline. `TwitterClient` does not store
  tokens; the auth helper refreshes and the worker writes back.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

X_API_BASE = "https://api.x.com/2"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class TwitterClient:
    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            r = await client.get(f"{X_API_BASE}{path}", params=params, headers=self._headers)
            r.raise_for_status()
            return r.json()

    # --- Identity ---

    async def get_me(self) -> dict[str, Any]:
        return await self._get(
            "/users/me",
            {"user.fields": "id,username,name,public_metrics"},
        )

    # --- Follows ---

    async def list_following(self, user_id: str, max_results: int = 1000) -> list[dict[str, Any]]:
        """Yield handles the user follows. Paginates internally up to `max_results`.

        X v2 caps `max_results` per page at 1000.
        """
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "max_results": min(1000, max_results - len(out)),
                "user.fields": "id,username,name,public_metrics",
            }
            if cursor:
                params["pagination_token"] = cursor
            data = await self._get(f"/users/{user_id}/following", params)
            out.extend(data.get("data", []))
            cursor = data.get("meta", {}).get("next_token")
            if not cursor or len(out) >= max_results:
                break
        return out


class AppOnlyTwitterClient:
    """App-only Bearer client for the full-archive search endpoints.

    `/tweets/search/all` and `/tweets/counts/all` require app-only auth — they
    reject user-context tokens. Mint the bearer once via the client_credentials
    grant; it doesn't expire.
    """

    # X enforces 1 req/sec on /tweets/search/all and /tweets/counts/all.
    # Process-wide lock + min-spacing prevents 429s when arq runs >1 batch in
    # parallel or when pagination fires back-to-back.
    _MIN_REQUEST_INTERVAL_S = 1.1
    _throttle_lock = asyncio.Lock()
    _last_request_at: float = 0.0

    def __init__(self, app_bearer: str) -> None:
        self._bearer = app_bearer

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer}"}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # Up to 5 attempts, exponentially backing off on 429.
        backoff = 1.0
        for attempt in range(5):
            async with AppOnlyTwitterClient._throttle_lock:
                now = asyncio.get_event_loop().time()
                wait = AppOnlyTwitterClient._last_request_at + self._MIN_REQUEST_INTERVAL_S - now
                if wait > 0:
                    await asyncio.sleep(wait)
                AppOnlyTwitterClient._last_request_at = asyncio.get_event_loop().time()

            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                r = await client.get(f"{X_API_BASE}{path}", params=params, headers=self._headers)
            if r.status_code == 429:
                # Honour Retry-After if present, otherwise exponential backoff.
                retry_after = float(r.headers.get("retry-after") or backoff)
                await asyncio.sleep(min(retry_after, 30.0))
                backoff = min(backoff * 2, 30.0)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()  # last response, will raise
        return r.json()

    @staticmethod
    def _build_query(handles: list[str]) -> str:
        # ~512-char query cap on self-serve full-archive. 25 handles fits comfortably.
        from_clause = " OR ".join(f"from:{h}" for h in handles)
        return f"({from_clause}) has:cashtags -is:retweet"

    async def search_kol_calls(
        self,
        handles: list[str],
        since_id: str | None = None,
        start_time: datetime | None = None,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Full-archive search for cashtag-bearing posts from a batch of handles.

        Returns matching tweets only — engagement floor is applied by the caller
        on `public_metrics.like_count` because v2 has no min_faves operator.
        """
        query = self._build_query(handles)
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "query": query,
                "max_results": 500,
                "tweet.fields": "id,text,created_at,author_id,public_metrics,entities,note_tweet",
            }
            if since_id:
                params["since_id"] = since_id
            elif start_time:
                params["start_time"] = start_time.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            if cursor:
                params["next_token"] = cursor
            data = await self._get("/tweets/search/all", params)
            out.extend(data.get("data", []))
            cursor = data.get("meta", {}).get("next_token")
            if not cursor:
                break
        return out

    async def count_kol_calls(
        self,
        handles: list[str],
        start_time: datetime,
        end_time: datetime,
    ) -> int:
        """Pre-flight cost estimator. Returns total matching posts in window.

        /tweets/counts/all returns counts only — no posts are billed against
        post-credits.
        """
        # X rejects end_time within 10s of request time; clamp to ~30s ago.
        end_safe = min(end_time, datetime.now(timezone.utc) - timedelta(seconds=30))
        params = {
            "query": self._build_query(handles),
            "granularity": "day",
            "start_time": start_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time": end_safe.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        data = await self._get("/tweets/counts/all", params)
        return int((data.get("meta") or {}).get("total_tweet_count", 0))


async def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, Any]:
    """Exchange a refresh token for a fresh access token. Confidential client (Basic auth)."""
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        r = await client.post(
            "https://api.x.com/2/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            auth=(client_id, client_secret) if client_secret else None,
        )
        r.raise_for_status()
        tokens = r.json()
        if "expires_at" not in tokens and "expires_in" in tokens:
            tokens["expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])
            ).isoformat()
        return tokens

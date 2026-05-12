"""Async CoinGecko v3 client.

Demo API key (free tier, 30 req/min) is sent via the `x-cg-demo-api-key`
header when `COINGECKO_API_KEY` is set in the environment. Without a key
the cloud-IP throttle drops the effective ceiling to ~10–15 req/min, so
even a free key roughly doubles throughput.

Endpoints used:
- /coins/list (id, symbol, name) — cached daily
- /coins/markets (top-1000 by mcap, used for ticker disambiguation)
- /coins/{id}/market_chart/range (vs_currency=usd; resolution depends on
  the gap to `now`):
    - <=1h     → minute granularity
    - <=24h    → 5-minute granularity
    - <=90d    → hourly granularity
    - >90d     → daily granularity
- /coins/{id}/contract/{addr} (used by the resolver for raw contract hits)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings

CG_BASE = "https://api.coingecko.com/api/v3"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Pacing knob. With a demo API key CG documents 30/min hard cap; we keep
# 4s = 15/min sustained to leave headroom for retries on bursty resolves.
_MIN_INTERVAL = 4.0  # seconds between calls
_last_call_at: float = 0.0
_lock = asyncio.Lock()


class CoinGeckoRateLimited(Exception):
    """Raised when /search retries are exhausted on 429.

    Callers should treat this as recoverable (CG is throttling, try later)
    rather than as a data error. The resolver-pending sweeper retries.
    """


def _auth_headers() -> dict[str, str]:
    """Demo-tier auth: `x-cg-demo-api-key`. Pro tier would use `x-cg-pro-api-key`."""
    key = get_settings().coingecko_api_key
    return {"x-cg-demo-api-key": key} if key else {}


async def _throttle() -> None:
    global _last_call_at
    async with _lock:
        now = asyncio.get_event_loop().time()
        wait = _MIN_INTERVAL - (now - _last_call_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_at = asyncio.get_event_loop().time()


async def _get(path: str, params: dict[str, Any] | None = None, *, retries: int = 5) -> Any:
    backoff = 5.0
    headers = _auth_headers()
    for attempt in range(retries):
        await _throttle()
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            r = await client.get(f"{CG_BASE}{path}", params=params, headers=headers)
            if r.status_code == 429:
                retry_after = float(r.headers.get("retry-after") or backoff)
                await asyncio.sleep(min(retry_after, 60.0))
                backoff = min(backoff * 2, 60.0)
                continue
            r.raise_for_status()
            return r.json()
    raise CoinGeckoRateLimited(f"coingecko: too many 429s for {path}")


# --- Token list / markets ---

async def list_all_coins() -> list[dict[str, Any]]:
    """Full ids/symbols/names. ~13k entries. Cache aggressively in caller."""
    return await _get("/coins/list")


async def top_markets(limit: int = 1000) -> list[dict[str, Any]]:
    """Top-N by market cap, used for ticker disambiguation."""
    pages = (limit + 249) // 250
    out: list[dict[str, Any]] = []
    for p in range(1, pages + 1):
        data = await _get(
            "/coins/markets",
            {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": p},
        )
        out.extend(data)
        if len(data) < 250:
            break
    return out[:limit]


async def lookup_by_contract(chain: str, address: str) -> dict[str, Any] | None:
    """Resolve a contract address to a CG coin entry. None if unknown."""
    try:
        return await _get(f"/coins/{chain}/contract/{address}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise


async def search(query: str) -> dict[str, Any]:
    """CG /search. Returns {'coins': [{id, name, symbol, market_cap_rank, ...}, ...], ...}."""
    return await _get("/search", {"query": query})


async def coin_market_data(coingecko_id: str) -> dict[str, Any] | None:
    """Lightweight market-cap fetch for a single CG id. Used to tiebreak ambiguous symbols."""
    try:
        return await _get(
            f"/coins/{coingecko_id}",
            {
                "localization": "false",
                "tickers": "false",
                "community_data": "false",
                "developer_data": "false",
                "sparkline": "false",
            },
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise


# --- Prices ---

async def market_chart_range(
    coingecko_id: str,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, float]]:
    """Returns [(ts, close_usd), ...]. Granularity is implicit in the (start,end) gap.

    CoinGecko returns `prices: [[ms, usd], ...]`.
    """
    params = {
        "vs_currency": "usd",
        "from": int(start.astimezone(timezone.utc).timestamp()),
        "to": int(end.astimezone(timezone.utc).timestamp()),
    }
    data = await _get(f"/coins/{coingecko_id}/market_chart/range", params)
    out: list[tuple[datetime, float]] = []
    for ms, price in data.get("prices", []):
        ts = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        out.append((ts, float(price)))
    return out

"""Resolve a parsed `TokenMatch` to a `tokens` row (creating it if needed).

Strategy:
- Contract: hit CG `/coins/{chain}/contract/{addr}`. Cache by (chain, addr).
- Ticker: query CG `/search?query=$SYM`, filter to exact symbol match,
  pick the candidate with the highest market_cap_rank (lowest rank number).
  This recovers ambiguous tickers (e.g. MEGA) that the prior top-1000 path
  silently dropped, and recovers fresh memecoins that haven't entered the
  top-1000 by mcap.

Caches live in Redis (24h TTL on /search results, 7d on contract lookups).
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import coingecko
from app.models import Token
from app.services.parsing import TokenMatch

_CG_CHAIN_MAP = {
    "ethereum": "ethereum",
    "solana": "solana",
}


async def _search_symbol_cached(redis: Any, sym: str) -> list[dict[str, Any]]:
    """Return CG /search 'coins' filtered to exact symbol match.

    Hits cached 24h, misses cached 1h — enough to stop spam $XYZ tickers
    re-burning CG quota, short enough that a newly-listed token gets picked
    up within the hour.
    """
    cache_key = f"cg:search:{sym}"
    cached = await redis.get(cache_key)
    if cached is not None:
        return json.loads(cached)
    data = await coingecko.search(sym)
    coins = [
        c for c in (data.get("coins") or []) if (c.get("symbol") or "").upper() == sym
    ]
    ttl = 24 * 3600 if coins else 3600
    await redis.set(cache_key, json.dumps(coins), ex=ttl)
    return coins


async def resolve(
    match: TokenMatch,
    session: AsyncSession,
    redis: Any,
) -> Token | None:
    """Idempotently return a Token row for the match, or None if unresolvable."""
    if match.kind == "contract":
        return await _resolve_contract(match, session, redis)
    return await _resolve_ticker(match, session, redis)


async def _resolve_contract(
    match: TokenMatch, session: AsyncSession, redis: Any
) -> Token | None:
    if not match.chain:
        return None
    cg_chain = _CG_CHAIN_MAP.get(match.chain, match.chain)
    addr = match.normalized()

    existing = (
        await session.execute(
            select(Token).where(Token.chain == match.chain, Token.contract_addr == addr)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    cache_key = f"cg:contract:{cg_chain}:{addr}"
    cached = await redis.get(cache_key)
    if cached:
        info = json.loads(cached) or None
    else:
        info = await coingecko.lookup_by_contract(cg_chain, addr)
        await redis.set(cache_key, json.dumps(info), ex=7 * 24 * 3600)

    if not info:
        return None

    token = Token(
        coingecko_id=info.get("id"),
        symbol=(info.get("symbol") or "").upper(),
        name=info.get("name"),
        contract_addr=addr,
        chain=match.chain,
        is_verified=True,
    )
    session.add(token)
    await session.flush()
    return token


async def _resolve_ticker(
    match: TokenMatch, session: AsyncSession, redis: Any
) -> Token | None:
    sym = match.normalized()
    existing = (
        await session.execute(
            select(Token).where(Token.symbol == sym, Token.contract_addr.is_(None))
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    candidates = await _search_symbol_cached(redis, sym)
    if not candidates:
        return None

    # /search results carry market_cap_rank (lower = bigger). Unranked → push last.
    def _rank(c: dict[str, Any]) -> int:
        r = c.get("market_cap_rank")
        return r if isinstance(r, int) else 10**9

    candidates.sort(key=_rank)
    info = candidates[0]

    token = Token(
        coingecko_id=info.get("id"),
        symbol=sym,
        name=info.get("name"),
        contract_addr=None,
        chain=None,
        is_verified=_rank(info) < 10**9,
    )
    session.add(token)
    await session.flush()
    return token

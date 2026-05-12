"""Resolve a parsed `TokenMatch` to a `tokens` row (creating it if needed).

Three-tier ticker resolution:
  Tier 1 — DB hit. tokens.symbol == sym AND contract_addr IS NULL → use it.
           (After pre-priming top-1000 this catches the vast majority.)
  Tier 2 — /search returns exactly one symbol-exact candidate → resolve.
  Tier 3 — multiple candidates:
    3a — account_token_aliases has a prior (account_id, symbol) → use that
         token. This is how memecoin shillers route to the right token:
         once an account drops a contract alongside the cashtag, all
         subsequent cashtags from that account use the same token.
    3b — top survivor rank < 500 AND second > 3× top's rank → resolve
         by mcap (clear winner, e.g. official-trump vs maga).
    3c — no clear winner → return AMBIGUOUS with the candidate list.
         Caller writes a mention with token_id=NULL and the candidates
         JSONB. Leaderboard ignores token_id IS NULL.

Contract matches stay 1:1 (one CG row per chain+address). They feed the
alias table from the worker after a successful resolve.

Caches live in Redis: 24h on /search hits, 1h on /search misses, 7d on
contract lookups.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
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

# Tier 3b decisiveness thresholds.
CLEAR_WINNER_TOP_RANK = 500       # top survivor must be inside top-500 mcap
CLEAR_WINNER_RANK_GAP = 3.0       # AND second survivor's rank > 3× top's


@dataclass
class ResolveOutcome:
    """Result of resolve(). Exactly one of `token` or `ambiguous` is set
    when resolution had a usable signal; both None == no match at all."""
    token: Token | None = None
    ambiguous: list[dict[str, Any]] | None = None  # [{id, name, symbol, market_cap_rank}, ...]


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


def _rank(c: dict[str, Any]) -> int:
    r = c.get("market_cap_rank")
    return r if isinstance(r, int) else 10**9


def _is_clear_winner(sorted_candidates: list[dict[str, Any]]) -> bool:
    """True when there's a decisive mcap lead — top is top-500 AND
    second is at least 3× the top's rank (smaller mcap)."""
    if len(sorted_candidates) < 2:
        return True
    top, second = _rank(sorted_candidates[0]), _rank(sorted_candidates[1])
    return top < CLEAR_WINNER_TOP_RANK and second > top * CLEAR_WINNER_RANK_GAP


async def resolve(
    match: TokenMatch,
    session: AsyncSession,
    redis: Any,
    account_id: int | None = None,
) -> ResolveOutcome:
    """Idempotently resolve a TokenMatch. account_id enables Tier 3a alias lookup."""
    if match.kind == "contract":
        token = await _resolve_contract(match, session, redis)
        return ResolveOutcome(token=token)
    return await _resolve_ticker(match, session, redis, account_id)


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
    match: TokenMatch,
    session: AsyncSession,
    redis: Any,
    account_id: int | None,
) -> ResolveOutcome:
    sym = match.normalized()

    # Tier 1 — DB hit (pre-primed top-1000 lives here).
    existing = (
        await session.execute(
            select(Token).where(Token.symbol == sym, Token.contract_addr.is_(None))
        )
    ).scalar_one_or_none()
    if existing:
        return ResolveOutcome(token=existing)

    # Tier 3a — per-account alias takes precedence over any /search guess.
    # We check before the network call so we don't burn a CG /search slot
    # on a symbol we already know how to route for this account.
    if account_id is not None:
        alias_token = await _alias_lookup(session, account_id, sym)
        if alias_token is not None:
            return ResolveOutcome(token=alias_token)

    candidates = await _search_symbol_cached(redis, sym)
    if not candidates:
        return ResolveOutcome()  # no match at all

    candidates.sort(key=_rank)

    # Tier 2 — single symbol-exact survivor.
    if len(candidates) == 1:
        info = candidates[0]
        token = await _insert_ticker_token(session, sym, info)
        return ResolveOutcome(token=token)

    # Tier 3b — clear mcap winner.
    if _is_clear_winner(candidates):
        info = candidates[0]
        token = await _insert_ticker_token(session, sym, info)
        return ResolveOutcome(token=token)

    # Tier 3c — no clear winner. Return the (trimmed) candidate list so the
    # caller can store it on the mention for later disambiguation.
    trimmed = [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "symbol": (c.get("symbol") or "").upper(),
            "market_cap_rank": c.get("market_cap_rank"),
        }
        for c in candidates[:10]  # cap; /search rarely returns >25, 10 is plenty
    ]
    return ResolveOutcome(ambiguous=trimmed)


async def _insert_ticker_token(
    session: AsyncSession, sym: str, info: dict[str, Any]
) -> Token:
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


async def _alias_lookup(
    session: AsyncSession, account_id: int, sym: str
) -> Token | None:
    """Tier 3a: if this (account, symbol) was previously paired with a
    contract address by the same author, route the cashtag to that token."""
    from sqlalchemy import text as sa_text
    row = (
        await session.execute(
            sa_text(
                "SELECT t.* FROM tokens t "
                "JOIN account_token_aliases a ON a.token_id = t.id "
                "WHERE a.account_id = :aid AND a.symbol = :sym"
            ),
            {"aid": account_id, "sym": sym},
        )
    ).mappings().first()
    if not row:
        return None
    # Hydrate a Token object from the row mapping.
    token = (
        await session.execute(select(Token).where(Token.id == row["id"]))
    ).scalar_one_or_none()
    return token

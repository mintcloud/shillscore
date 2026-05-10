"""Admin endpoints — read-only inspection of DB + queue state.

Drop at: apps/api/app/routers/admin.py
Mount in main.py:
    from app.routers import admin
    app.include_router(admin.router, prefix="/api/admin")
"""
from __future__ import annotations

import os

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Account, Mention, Token, User, UserFollow

router = APIRouter(tags=["admin"])

ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "")


def _require_admin(x_admin_token: str = Header(default="")) -> None:
    if not ADMIN_API_TOKEN or x_admin_token != ADMIN_API_TOKEN:
        raise HTTPException(status_code=401, detail="admin token invalid")


async def _redis() -> aioredis.Redis:
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


@router.get("/stats", dependencies=[Depends(_require_admin)])
async def stats(session: AsyncSession = Depends(get_session)) -> dict:
    counts = {}
    for label, model in (
        ("accounts", Account),
        ("tokens", Token),
        ("mentions", Mention),
        ("users", User),
        ("follows", UserFollow),
    ):
        counts[label] = (await session.execute(select(func.count()).select_from(model))).scalar_one()

    async def _count(redis: aioredis.Redis, pattern: str) -> int:
        n = 0
        async for _ in redis.scan_iter(match=pattern, count=500):
            n += 1
        return n

    r = await _redis()
    try:
        queue = await r.llen("arq:queue")
        in_progress = await _count(r, "arq:in-progress:*")
        retry = await _count(r, "arq:retry:*")
        results = await _count(r, "arq:result:*")
    finally:
        await r.aclose()

    return {
        "counts": counts,
        "queue": {"pending": queue, "in_progress": in_progress, "retry": retry, "results": results},
    }


@router.get("/accounts", dependencies=[Depends(_require_admin)])
async def accounts(session: AsyncSession = Depends(get_session), limit: int = 200) -> dict:
    sub = (
        select(Mention.account_id, func.count().label("n"))
        .group_by(Mention.account_id)
        .subquery()
    )
    q = (
        select(
            Account.handle,
            Account.followers_count,
            Account.last_synced_at,
            Account.last_tweet_id,
            Account.oldest_tweet_id,
            func.coalesce(sub.c.n, 0).label("mentions"),
        )
        .join(sub, sub.c.account_id == Account.id, isouter=True)
        .order_by(desc(Account.last_synced_at).nulls_last())
        .limit(limit)
    )
    rows = (await session.execute(q)).mappings().all()
    return {"rows": [dict(r) for r in rows]}


@router.get("/mentions", dependencies=[Depends(_require_admin)])
async def mentions(session: AsyncSession = Depends(get_session), limit: int = 50) -> dict:
    q = (
        select(
            Mention.id,
            Mention.tweet_id,
            Mention.tweet_ts,
            Mention.tweet_text,
            Mention.raw_match,
            Mention.match_kind,
            Mention.sentiment,
            Account.handle.label("handle"),
            Token.symbol.label("symbol"),
            Token.coingecko_id.label("coingecko_id"),
        )
        .join(Account, Account.id == Mention.account_id)
        .join(Token, Token.id == Mention.token_id, isouter=True)
        .order_by(desc(Mention.tweet_ts))
        .limit(limit)
    )
    rows = (await session.execute(q)).mappings().all()
    return {"rows": [dict(r) for r in rows]}


@router.get("/tokens", dependencies=[Depends(_require_admin)])
async def tokens(session: AsyncSession = Depends(get_session), limit: int = 100) -> dict:
    q = (
        select(
            Token.symbol,
            Token.name,
            Token.coingecko_id,
            Token.contract_addr,
            Token.is_verified,
            func.count(Mention.id).label("mentions"),
        )
        .join(Mention, Mention.token_id == Token.id, isouter=True)
        .group_by(Token.id)
        .order_by(desc(func.count(Mention.id)))
        .limit(limit)
    )
    rows = (await session.execute(q)).mappings().all()
    return {"rows": [dict(r) for r in rows]}

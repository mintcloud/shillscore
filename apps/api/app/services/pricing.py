"""Price-window selection + upsert helpers.

Per plan §3: fresh mentions (<23h old) get a 5-min ±2h slab; aged get
hourly ±24h. The anchor is the closest bucket to `tweet_ts`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import coingecko
from app.models import BenchmarkPrice, Mention, Token, TokenPrice

FRESH_WINDOW_HOURS = 23


def _window_for_age(tweet_ts: datetime) -> tuple[datetime, datetime, str]:
    """Return (start, end, granularity_label) for the anchor fetch."""
    now = datetime.now(timezone.utc)
    age = now - tweet_ts
    if age < timedelta(hours=FRESH_WINDOW_HOURS):
        return tweet_ts - timedelta(hours=2), tweet_ts + timedelta(hours=2), "5min"
    return tweet_ts - timedelta(hours=24), tweet_ts + timedelta(hours=24), "hourly"


async def fetch_and_upsert_anchor(
    session: AsyncSession,
    token: Token,
    mention: Mention,
) -> None:
    """Fetch the slab around `mention.tweet_ts` and write the anchor back."""
    if not token.coingecko_id:
        return

    start, end, gran = _window_for_age(mention.tweet_ts)
    series = await coingecko.market_chart_range(token.coingecko_id, start, end)
    if not series:
        # Fallback: closest daily close at-or-before tweet_ts (anchor_kind='daily-fallback').
        series_daily = await coingecko.market_chart_range(
            token.coingecko_id,
            mention.tweet_ts - timedelta(days=2),
            mention.tweet_ts + timedelta(days=2),
        )
        if not series_daily:
            return
        await _upsert_prices(session, token.id, series_daily, "daily", "coingecko")
        anchor_ts, anchor_px = _closest(series_daily, mention.tweet_ts)
        mention.price_at_mention = Decimal(str(anchor_px))
        mention.price_at_mention_ts = anchor_ts
        mention.price_anchor_kind = "daily-fallback"
        mention.price_source = "coingecko"
        return

    await _upsert_prices(session, token.id, series, gran, "coingecko")
    anchor_ts, anchor_px = _closest(series, mention.tweet_ts)
    mention.price_at_mention = Decimal(str(anchor_px))
    mention.price_at_mention_ts = anchor_ts
    mention.price_anchor_kind = gran
    mention.price_source = "coingecko"


def _closest(series: list[tuple[datetime, float]], target: datetime) -> tuple[datetime, float]:
    return min(series, key=lambda r: abs((r[0] - target).total_seconds()))


async def _upsert_prices(
    session: AsyncSession,
    token_id: int,
    series: list[tuple[datetime, float]],
    granularity: str,
    source: str,
) -> None:
    if not series:
        return
    rows = [
        {
            "token_id": token_id,
            "ts": ts,
            "close_usd": Decimal(str(px)),
            "granularity": granularity,
            "source": source,
        }
        for ts, px in series
    ]
    stmt = insert(TokenPrice).values(rows).on_conflict_do_nothing(
        index_elements=["token_id", "ts", "granularity"]
    )
    await session.execute(stmt)


async def extend_daily_series(session: AsyncSession, token: Token) -> int:
    """Top up the daily continuous series for a token. Returns rows added."""
    if not token.coingecko_id:
        return 0
    last_ts = (
        await session.execute(
            select(TokenPrice.ts)
            .where(TokenPrice.token_id == token.id, TokenPrice.granularity == "daily")
            .order_by(TokenPrice.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    start = last_ts or datetime.now(timezone.utc) - timedelta(days=365)
    end = datetime.now(timezone.utc)
    if (end - start).total_seconds() < 23 * 3600:
        return 0  # already topped up today
    series = await coingecko.market_chart_range(token.coingecko_id, start, end)
    await _upsert_prices(session, token.id, series, "daily", "coingecko")
    return len(series)


async def refresh_benchmarks(session: AsyncSession) -> int:
    """Top up BTC + ETH daily benchmarks. Returns total rows added."""
    total = 0
    for symbol, cg_id in (("BTC", "bitcoin"), ("ETH", "ethereum")):
        last_ts = (
            await session.execute(
                select(BenchmarkPrice.ts)
                .where(BenchmarkPrice.symbol == symbol)
                .order_by(BenchmarkPrice.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        start = last_ts or datetime.now(timezone.utc) - timedelta(days=365)
        end = datetime.now(timezone.utc)
        if (end - start).total_seconds() < 23 * 3600:
            continue
        series = await coingecko.market_chart_range(cg_id, start, end)
        if not series:
            continue
        rows = [
            {"symbol": symbol, "ts": ts, "close_usd": Decimal(str(px))}
            for ts, px in series
        ]
        stmt = insert(BenchmarkPrice).values(rows).on_conflict_do_nothing(
            index_elements=["symbol", "ts"]
        )
        await session.execute(stmt)
        total += len(rows)
    return total

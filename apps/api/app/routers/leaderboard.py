"""Public leaderboard + account + mention endpoints.

Reads from the cohort-parameterized materialized view `account_leaderboard_cohort`
(see migration 0004). Ranking uses sqrt(N) damping so that low-N accounts don't
dominate the top of the table — `damped = median_excess * sqrt(n / (n + k))`,
k = 5. CI bands come from `account_ci` (still 365d-only for now; nightly job
will be extended in a follow-up if 30d ranks ever need bootstrapped CIs).

Cohorts: 30d (default), 90d, 365d.
Sort: excess (BTC-excess return, default) or raw (token return).
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session

router = APIRouter(tags=["leaderboard"])

Cohort = Literal["30d", "90d", "365d"]
Sort = Literal["excess", "raw"]
DAMP_K = 5  # sqrt(N / (N + k)) damping constant
MIN_N = DAMP_K  # min matured calls to appear on leaderboard — matches DAMP_K so
                # the data weight in damping is at least equal to the prior


def _damp(n: int) -> float:
    return math.sqrt(n / (n + DAMP_K))


def excluded_dominant_cte(cohort: Cohort) -> str:
    """SQL CTE chain that aggregates per-handle stats after dropping each
    handle's #1-most-mentioned token. Exposes a final CTE named `agg` with
    columns matching account_leaderboard_cohort (sans `cohort`).
    """
    excess_col = f"r_{cohort}_excess"
    raw_col = f"r_{cohort}"
    closed_col = f"is_closed_{cohort}"
    return f"""
    WITH cohort_mentions AS (
      SELECT mr.account_id, mr.token_id,
             mr.{raw_col} AS r_raw, mr.{excess_col} AS r_excess
      FROM mention_returns mr
      WHERE mr.{closed_col} AND mr.{excess_col} IS NOT NULL
    ),
    token_counts AS (
      SELECT account_id, token_id, count(*) AS cnt
      FROM cohort_mentions
      GROUP BY account_id, token_id
    ),
    dominant_token AS (
      SELECT DISTINCT ON (account_id) account_id, token_id
      FROM token_counts
      ORDER BY account_id, cnt DESC, token_id
    ),
    filtered AS (
      SELECT cm.account_id, cm.r_raw, cm.r_excess
      FROM cohort_mentions cm
      JOIN dominant_token dt ON dt.account_id = cm.account_id
      WHERE cm.token_id != dt.token_id
    ),
    agg AS (
      SELECT a.id AS account_id, a.handle, a.display_name, a.followers_count,
             count(*) AS n_closed,
             count(*) FILTER (WHERE f.r_excess > 0) AS n_winners,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY f.r_excess) AS median_excess,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY f.r_raw)    AS median_raw,
             avg(f.r_excess)                                          AS mean_excess
      FROM filtered f
      JOIN accounts a ON a.id = f.account_id
      GROUP BY a.id, a.handle, a.display_name, a.followers_count
    )
    """


@router.get("/leaderboard")
async def get_leaderboard(
    cohort: Cohort = Query("30d"),
    sort: Sort = Query("excess"),
    limit: int = Query(100, ge=1, le=500),
    min_n: int = Query(MIN_N, ge=1, le=500),
    exclude_dominant_token: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sort_col = "median_excess" if sort == "excess" else "median_raw"
    if exclude_dominant_token:
        sql = f"""
            {excluded_dominant_cte(cohort)}
            SELECT account_id, handle, display_name, followers_count,
                   n_closed, n_winners, median_excess, median_raw, mean_excess,
                   NULL::double precision AS ci_low_excess,
                   NULL::double precision AS ci_high_excess
            FROM agg
            WHERE n_closed >= :min_n
            ORDER BY {sort_col} DESC NULLS LAST
            LIMIT :limit
        """
        rows = (
            await session.execute(text(sql), {"limit": limit, "min_n": min_n})
        ).mappings().all()
    else:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT lc.account_id, lc.handle, lc.display_name, lc.followers_count,
                           lc.n_closed, lc.n_winners,
                           lc.median_excess, lc.median_raw, lc.mean_excess,
                           ci.ci_low_excess, ci.ci_high_excess
                    FROM account_leaderboard_cohort lc
                    LEFT JOIN account_ci ci ON ci.account_id = lc.account_id
                    WHERE lc.cohort = :cohort
                      AND lc.n_closed >= :min_n
                    ORDER BY {sort_col} DESC NULLS LAST
                    LIMIT :limit
                    """
                ),
                {"cohort": cohort, "limit": limit, "min_n": min_n},
            )
        ).mappings().all()

    out = []
    for r in rows:
        n = int(r["n_closed"] or 0)
        median = float(r[sort_col]) if r[sort_col] is not None else None
        damped = median * _damp(n) if median is not None else None
        out.append(
            {
                "account_id": r["account_id"],
                "handle": r["handle"],
                "display_name": r["display_name"],
                "followers": r["followers_count"],
                "n_matured": n,
                "n_winners": int(r["n_winners"] or 0),
                "win_rate": (r["n_winners"] / n) if n else None,
                "median_excess": float(r["median_excess"]) if r["median_excess"] is not None else None,
                "median_raw": float(r["median_raw"]) if r["median_raw"] is not None else None,
                "mean_excess": float(r["mean_excess"]) if r["mean_excess"] is not None else None,
                "damped_score": damped,
                "ci_low_excess": float(r["ci_low_excess"]) if r["ci_low_excess"] is not None else None,
                "ci_high_excess": float(r["ci_high_excess"]) if r["ci_high_excess"] is not None else None,
            }
        )

    # Re-sort by damped score so low-N accounts sink. The SQL ORDER BY pulled top
    # candidates by raw median; damping reorders within that pool.
    out.sort(key=lambda x: (x["damped_score"] is None, -(x["damped_score"] or 0)))
    return {
        "cohort": cohort,
        "sort": sort,
        "exclude_dominant_token": exclude_dominant_token,
        "rows": out,
    }


@router.get("/account/{handle}")
async def get_account(
    handle: str,
    exclude_dominant_token: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> dict:
    handle = handle.lstrip("@").lower()
    account = (
        await session.execute(
            text(
                """
                SELECT id, handle, display_name, followers_count, last_synced_at,
                       lookback_days, first_seen_at
                FROM accounts WHERE lower(handle) = :h
                """
            ),
            {"h": handle},
        )
    ).mappings().first()
    if not account:
        raise HTTPException(status_code=404, detail=f"account @{handle} not found")

    aid = account["id"]

    if exclude_dominant_token:
        # Per-cohort: drop the handle's #1-most-mentioned token within that
        # cohort, then recompute median/mean/win-rate over the remaining
        # matured calls. Each cohort drops its own dominant token because the
        # dominant-token set differs by cohort window.
        cohort_summary: dict[str, dict] = {}
        for cohort_name in ("30d", "90d", "365d"):
            excess_col = f"r_{cohort_name}_excess"
            raw_col = f"r_{cohort_name}"
            closed_col = f"is_closed_{cohort_name}"
            sql = f"""
                WITH cohort_mentions AS (
                  SELECT mr.token_id,
                         mr.{raw_col} AS r_raw, mr.{excess_col} AS r_excess
                  FROM mention_returns mr
                  JOIN mentions m ON m.id = mr.id
                  WHERE m.account_id = :aid
                    AND mr.{closed_col} AND mr.{excess_col} IS NOT NULL
                ),
                token_counts AS (
                  SELECT token_id, count(*) AS cnt
                  FROM cohort_mentions
                  GROUP BY token_id
                ),
                dominant_token AS (
                  SELECT token_id FROM token_counts
                  ORDER BY cnt DESC, token_id
                  LIMIT 1
                ),
                filtered AS (
                  SELECT cm.r_raw, cm.r_excess FROM cohort_mentions cm
                  WHERE cm.token_id != (SELECT token_id FROM dominant_token)
                )
                SELECT count(*) AS n_closed,
                       count(*) FILTER (WHERE r_excess > 0) AS n_winners,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY r_excess) AS median_excess,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY r_raw)    AS median_raw,
                       avg(r_excess) AS mean_excess
                FROM filtered
            """
            row = (
                await session.execute(text(sql), {"aid": aid})
            ).mappings().first()
            n = int(row["n_closed"] or 0) if row else 0
            if n == 0:
                # Match the unfiltered behavior: omit cohorts with zero matured
                # calls so the UI shows "no matured calls" rather than n=0.
                continue
            med = float(row["median_excess"]) if row["median_excess"] is not None else None
            cohort_summary[cohort_name] = {
                "n_matured": n,
                "n_winners": int(row["n_winners"] or 0),
                "win_rate": (row["n_winners"] / n) if n else None,
                "median_excess": med,
                "median_raw": float(row["median_raw"]) if row["median_raw"] is not None else None,
                "mean_excess": float(row["mean_excess"]) if row["mean_excess"] is not None else None,
                "damped_score": med * _damp(n) if med is not None else None,
            }
    else:
        cohorts = (
            await session.execute(
                text(
                    """
                    SELECT cohort, n_closed, n_winners, median_excess, median_raw, mean_excess
                    FROM account_leaderboard_cohort
                    WHERE account_id = :aid
                    """
                ),
                {"aid": aid},
            )
        ).mappings().all()

        cohort_summary = {}
        for c in cohorts:
            n = int(c["n_closed"] or 0)
            med = float(c["median_excess"]) if c["median_excess"] is not None else None
            cohort_summary[c["cohort"]] = {
                "n_matured": n,
                "n_winners": int(c["n_winners"] or 0),
                "win_rate": (c["n_winners"] / n) if n else None,
                "median_excess": med,
                "median_raw": float(c["median_raw"]) if c["median_raw"] is not None else None,
                "mean_excess": float(c["mean_excess"]) if c["mean_excess"] is not None else None,
                "damped_score": med * _damp(n) if med is not None else None,
            }

    mentions = (
        await session.execute(
            text(
                """
                SELECT m.id, m.tweet_id, m.tweet_ts, m.tweet_text,
                       m.raw_match, m.match_kind, m.sentiment, m.price_at_mention,
                       t.symbol, t.coingecko_id, t.contract_addr,
                       mr.r_1d, mr.r_7d, mr.r_30d, mr.r_90d, mr.r_365d,
                       mr.r_30d_excess, mr.r_90d_excess, mr.r_365d_excess,
                       mr.is_closed_30d, mr.is_closed_90d, mr.is_closed_365d
                FROM mentions m
                LEFT JOIN tokens t ON t.id = m.token_id
                LEFT JOIN mention_returns mr ON mr.id = m.id
                WHERE m.account_id = :aid
                ORDER BY m.tweet_ts DESC
                LIMIT 500
                """
            ),
            {"aid": aid},
        )
    ).mappings().all()

    def _f(v):
        return float(v) if v is not None else None

    return {
        "account": {
            "handle": account["handle"],
            "display_name": account["display_name"],
            "followers": account["followers_count"],
            "last_synced_at": account["last_synced_at"].isoformat() if account["last_synced_at"] else None,
            "lookback_days": account["lookback_days"],
            "first_seen_at": account["first_seen_at"].isoformat() if account["first_seen_at"] else None,
        },
        "cohorts": cohort_summary,
        "mentions": [
            {
                "id": m["id"],
                "tweet_id": m["tweet_id"],
                "tweet_ts": m["tweet_ts"].isoformat() if m["tweet_ts"] else None,
                "tweet_text": m["tweet_text"],
                "raw_match": m["raw_match"],
                "match_kind": m["match_kind"],
                "sentiment": m["sentiment"],
                "price_at_mention": _f(m["price_at_mention"]),
                "symbol": m["symbol"],
                "coingecko_id": m["coingecko_id"],
                "contract_addr": m["contract_addr"],
                "returns": {
                    "r_1d": _f(m["r_1d"]),
                    "r_7d": _f(m["r_7d"]),
                    "r_30d": _f(m["r_30d"]),
                    "r_90d": _f(m["r_90d"]),
                    "r_365d": _f(m["r_365d"]),
                    "r_30d_excess": _f(m["r_30d_excess"]),
                    "r_90d_excess": _f(m["r_90d_excess"]),
                    "r_365d_excess": _f(m["r_365d_excess"]),
                },
                "matured": {
                    "30d": bool(m["is_closed_30d"]) if m["is_closed_30d"] is not None else False,
                    "90d": bool(m["is_closed_90d"]) if m["is_closed_90d"] is not None else False,
                    "365d": bool(m["is_closed_365d"]) if m["is_closed_365d"] is not None else False,
                },
            }
            for m in mentions
        ],
    }


@router.get("/mention/{mention_id}")
async def get_mention(
    mention_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = (
        await session.execute(
            text(
                """
                SELECT m.id, m.tweet_id, m.tweet_ts, m.tweet_text, m.raw_match,
                       m.match_kind, m.sentiment, m.price_at_mention,
                       a.handle, a.display_name,
                       t.symbol, t.name AS token_name, t.coingecko_id, t.contract_addr,
                       mr.r_1d, mr.r_7d, mr.r_30d, mr.r_90d, mr.r_365d,
                       mr.r_30d_excess, mr.r_90d_excess, mr.r_365d_excess,
                       mr.is_closed_30d, mr.is_closed_90d, mr.is_closed_365d
                FROM mentions m
                JOIN accounts a ON a.id = m.account_id
                LEFT JOIN tokens t ON t.id = m.token_id
                LEFT JOIN mention_returns mr ON mr.id = m.id
                WHERE m.id = :mid
                """
            ),
            {"mid": mention_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"mention {mention_id} not found")

    def _f(v):
        return float(v) if v is not None else None

    return {
        "id": row["id"],
        "tweet_id": row["tweet_id"],
        "tweet_ts": row["tweet_ts"].isoformat() if row["tweet_ts"] else None,
        "tweet_text": row["tweet_text"],
        "raw_match": row["raw_match"],
        "match_kind": row["match_kind"],
        "sentiment": row["sentiment"],
        "price_at_mention": _f(row["price_at_mention"]),
        "account": {"handle": row["handle"], "display_name": row["display_name"]},
        "token": {
            "symbol": row["symbol"],
            "name": row["token_name"],
            "coingecko_id": row["coingecko_id"],
            "contract_addr": row["contract_addr"],
        },
        "returns": {
            "r_1d": _f(row["r_1d"]),
            "r_7d": _f(row["r_7d"]),
            "r_30d": _f(row["r_30d"]),
            "r_90d": _f(row["r_90d"]),
            "r_365d": _f(row["r_365d"]),
            "r_30d_excess": _f(row["r_30d_excess"]),
            "r_90d_excess": _f(row["r_90d_excess"]),
            "r_365d_excess": _f(row["r_365d_excess"]),
        },
        "matured": {
            "30d": bool(row["is_closed_30d"]) if row["is_closed_30d"] is not None else False,
            "90d": bool(row["is_closed_90d"]) if row["is_closed_90d"] is not None else False,
            "365d": bool(row["is_closed_365d"]) if row["is_closed_365d"] is not None else False,
        },
    }


@router.get("/mention/{mention_id}/series")
async def get_mention_series(
    mention_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mixed-granularity price series for the chart. Returns whatever's stored:
    5-minute slab around t0 if present, daily series afterwards.
    """
    mention = (
        await session.execute(
            text(
                """
                SELECT m.id, m.token_id, m.tweet_ts, m.price_at_mention
                FROM mentions m WHERE m.id = :mid
                """
            ),
            {"mid": mention_id},
        )
    ).mappings().first()
    if not mention:
        raise HTTPException(status_code=404, detail=f"mention {mention_id} not found")

    if mention["token_id"] is None:
        return {"mention_id": mention_id, "tweet_ts": mention["tweet_ts"].isoformat(), "points": []}

    points = (
        await session.execute(
            text(
                """
                SELECT ts, granularity, close_usd
                FROM token_prices
                WHERE token_id = :tid
                  AND ts BETWEEN :start AND :end
                ORDER BY ts ASC
                """
            ),
            {
                "tid": mention["token_id"],
                "start": mention["tweet_ts"] - timedelta(days=2),
                "end": mention["tweet_ts"] + timedelta(days=400),
            },
        )
    ).mappings().all()

    return {
        "mention_id": mention_id,
        "tweet_ts": mention["tweet_ts"].isoformat(),
        "p0": float(mention["price_at_mention"]) if mention["price_at_mention"] is not None else None,
        "points": [
            {
                "ts": p["ts"].isoformat(),
                "granularity": p["granularity"],
                "close_usd": float(p["close_usd"]),
            }
            for p in points
        ],
    }

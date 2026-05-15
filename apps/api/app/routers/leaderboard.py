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
View = Literal["scouts", "insiders", "all"]
DAMP_K = 5  # sqrt(N / (N + k)) damping constant
MIN_N = DAMP_K  # min matured calls to appear on leaderboard — matches DAMP_K so
                # the data weight in damping is at least equal to the prior

# Path A — concentration split. A handle is an "insider" when at least this
# share of its matured calls in the cohort land on a single token, i.e. the
# score leans on one bag; below it the handle is a "scout" (diversified).
# 0.5 is a clean cut: with <50% on the top token a handle has necessarily
# called >=3 distinct tokens. Empirically (30d cohort) it isolates project
# accounts shilling their own coin — superform/UP, MANTRA, zksync, injective,
# worldlibertyfi — into insiders, and leaves analytics/scout accounts —
# lookonchain, nansen, arkham, AerodromeFi — as scouts. Unlike Path B's
# drop-the-#1-token hack, scores here are the honest full-record aggregate;
# the views only partition the population.
CONCENTRATION_THRESHOLD = 0.5


def _damp(n: int) -> float:
    return math.sqrt(n / (n + DAMP_K))


def concentration_cte(cohort: Cohort) -> str:
    """CTE fragment (no leading `WITH`) exposing a final CTE `concentration`
    with per-account token-concentration stats over matured calls in `cohort`:
    `account_id`, `n_distinct_tokens`, `top_token_id`, `top_token_share`.

    Concentration is measured over exactly the population the cohort score is
    computed from (matured calls with a non-null BTC-excess), so
    `top_token_share` reads as "this fraction of the handle's score comes from
    a single token".
    """
    excess_col = f"r_{cohort}_excess"
    closed_col = f"is_closed_{cohort}"
    return f"""
    cohort_mentions AS (
      SELECT account_id, token_id
      FROM mention_returns
      WHERE {closed_col} AND {excess_col} IS NOT NULL
    ),
    token_counts AS (
      SELECT account_id, token_id, count(*) AS cnt
      FROM cohort_mentions
      GROUP BY account_id, token_id
    ),
    top_token AS (
      SELECT DISTINCT ON (account_id)
             account_id, token_id AS top_token_id, cnt AS top_cnt
      FROM token_counts
      ORDER BY account_id, cnt DESC, token_id
    ),
    concentration AS (
      SELECT tc.account_id,
             count(*)                              AS n_distinct_tokens,
             tt.top_token_id,
             tt.top_cnt::float / sum(tc.cnt)        AS top_token_share
      FROM token_counts tc
      JOIN top_token tt ON tt.account_id = tc.account_id
      GROUP BY tc.account_id, tt.top_token_id, tt.top_cnt
    )
    """


def view_filter_sql(view: View) -> str:
    """WHERE-clause fragment partitioning the leaderboard by concentration.
    Expects the concentration CTE aliased as `c` and a `:threshold` bind param.
    """
    if view == "scouts":
        return "AND c.top_token_share < :threshold"
    if view == "insiders":
        return "AND c.top_token_share >= :threshold"
    return ""


@router.get("/leaderboard")
async def get_leaderboard(
    cohort: Cohort = Query("30d"),
    sort: Sort = Query("excess"),
    view: View = Query("scouts"),
    limit: int = Query(100, ge=1, le=500),
    min_n: int = Query(MIN_N, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cohort leaderboard, partitioned by concentration (Path A).

    `view` = scouts (default — top token < 50% of matured calls), insiders
    (>= 50%, i.e. score leans on one bag), or all. The score is always the
    honest full-record aggregate from `account_leaderboard_cohort`; the view
    only filters which handles appear, and each row carries its concentration
    stats so the bias is visible rather than hidden.
    """
    sort_col = "median_excess" if sort == "excess" else "median_raw"
    sql = f"""
        WITH {concentration_cte(cohort)}
        SELECT lc.account_id, lc.handle, lc.display_name, lc.followers_count,
               lc.n_closed, lc.n_winners,
               lc.median_excess, lc.median_raw, lc.mean_excess,
               ci.ci_low_excess, ci.ci_high_excess,
               c.n_distinct_tokens, c.top_token_share,
               t.symbol AS top_token_symbol
        FROM account_leaderboard_cohort lc
        JOIN concentration c ON c.account_id = lc.account_id
        LEFT JOIN tokens t ON t.id = c.top_token_id
        LEFT JOIN account_ci ci ON ci.account_id = lc.account_id
        WHERE lc.cohort = :cohort
          AND lc.n_closed >= :min_n
          {view_filter_sql(view)}
        ORDER BY {sort_col} DESC NULLS LAST
        LIMIT :limit
    """
    params: dict = {"cohort": cohort, "limit": limit, "min_n": min_n}
    if view != "all":
        params["threshold"] = CONCENTRATION_THRESHOLD
    rows = (await session.execute(text(sql), params)).mappings().all()

    out = []
    for r in rows:
        n = int(r["n_closed"] or 0)
        median = float(r[sort_col]) if r[sort_col] is not None else None
        damped = median * _damp(n) if median is not None else None
        share = float(r["top_token_share"]) if r["top_token_share"] is not None else None
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
                "n_distinct_tokens": int(r["n_distinct_tokens"] or 0),
                "top_token_symbol": r["top_token_symbol"],
                "top_token_share": share,
                "is_scout": share is not None and share < CONCENTRATION_THRESHOLD,
            }
        )

    # Re-sort by damped score so low-N accounts sink. The SQL ORDER BY pulled top
    # candidates by raw median; damping reorders within that pool.
    out.sort(key=lambda x: (x["damped_score"] is None, -(x["damped_score"] or 0)))
    return {
        "cohort": cohort,
        "sort": sort,
        "view": view,
        "concentration_threshold": CONCENTRATION_THRESHOLD,
        "rows": out,
    }


@router.get("/account/{handle}")
async def get_account(
    handle: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Per-account stats. Cohort summaries are the honest full-record aggregate
    (every matured call counts). Each cohort also carries a `concentration`
    block — top token, its share, distinct-token count, and the scout/insider
    flag — so the page can show *why* the handle is or isn't on the scouts
    leaderboard rather than silently re-scoring it.
    """
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

    cohort_summary: dict[str, dict] = {}
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

    # Per-cohort concentration — measured over the same matured-call population
    # the cohort score uses, so the share reads as "fraction of this handle's
    # score that comes from one token".
    for cohort_name, summary in cohort_summary.items():
        excess_col = f"r_{cohort_name}_excess"
        closed_col = f"is_closed_{cohort_name}"
        crow = (
            await session.execute(
                text(
                    f"""
                    WITH cm AS (
                      SELECT token_id FROM mention_returns
                      WHERE account_id = :aid
                        AND {closed_col} AND {excess_col} IS NOT NULL
                    ),
                    tc AS (SELECT token_id, count(*) AS cnt FROM cm GROUP BY token_id)
                    SELECT count(*) AS n_distinct_tokens,
                           max(cnt)::float / NULLIF(sum(cnt), 0) AS top_token_share,
                           (SELECT t.symbol FROM tc
                            JOIN tokens t ON t.id = tc.token_id
                            ORDER BY tc.cnt DESC, tc.token_id LIMIT 1) AS top_token_symbol
                    FROM tc
                    """
                ),
                {"aid": aid},
            )
        ).mappings().first()
        share = (
            float(crow["top_token_share"])
            if crow and crow["top_token_share"] is not None
            else None
        )
        summary["concentration"] = {
            "n_distinct_tokens": int(crow["n_distinct_tokens"] or 0) if crow else 0,
            "top_token_symbol": crow["top_token_symbol"] if crow else None,
            "top_token_share": share,
            "is_scout": share is not None and share < CONCENTRATION_THRESHOLD,
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
        "concentration_threshold": CONCENTRATION_THRESHOLD,
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

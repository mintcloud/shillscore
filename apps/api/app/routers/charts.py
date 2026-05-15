"""Chart endpoints — power the visualizations on the home + account pages.

Two families:

  /api/leaderboard/equity-curves
      Top-N accounts, each with a running-mean BTC-excess curve over calendar
      time. The "building the statistic" view: each matured call drags the
      account's line up or down. Endpoint = today's score.

  /api/account/{handle}/mention-curves
      One series per mention, anchored at t0 = tweet time, expressed in
      BTC-excess. Daily price snapshots vs the mention's price_at_mention,
      with the BTC drift subtracted. "Spaghetti" overlay for the account
      page.

  /api/leaderboard/token-charts
      Token-centric small-multiples: top-N tokens by BTC-excess return over
      the cohort window (measured from the *first* mention by any tracked
      account), each with its indexed price line plus per-account mention
      markers. Top-leaderboard accounts coloured, other tracked accounts
      greyed. Survivor-biased by construction — the chart is "who caught
      these winners", not skill.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.routers.leaderboard import (
    CONCENTRATION_THRESHOLD,
    MIN_N,
    View,
    concentration_cte,
    view_filter_sql,
)

router = APIRouter(tags=["charts"])

Cohort = Literal["30d", "90d", "365d"]

# Map cohort → return column to plot in the calendar-time curve.
_EXCESS_COL = {"30d": "r_30d_excess", "90d": "r_90d_excess", "365d": "r_365d_excess"}
_RAW_COL = {"30d": "r_30d", "90d": "r_90d", "365d": "r_365d"}
_CLOSED_COL = {"30d": "is_closed_30d", "90d": "is_closed_90d", "365d": "is_closed_365d"}
_HORIZON_DAYS = {"30d": 30, "90d": 90, "365d": 365}

# Excluded from the token-charts view — by definition these don't have
# returns to "catch", so they only crowd out real movers in the top-N.
_STABLECOIN_SYMBOLS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "USDD", "GUSD",
    "LUSD", "MIM", "FRAX", "FDUSD", "PYUSD", "USDE", "USDS", "RLUSD",
    "SUSD", "USTC", "UST", "CRVUSD", "GHO", "SUSDE", "SFRAX", "USDX",
    "EUSD", "USDB", "USDM", "USDY", "DOLA", "ALUSD", "MKUSD", "FEI",
}


def _f(v) -> float | None:
    return float(v) if v is not None else None


async def _running_mean_curve(
    session: AsyncSession,
    account_id: int,
    cohort: Cohort,
) -> list[dict]:
    """Calendar-time series of running-mean excess return for one account.

    Always over the honest full record — Path A partitions accounts into
    views, it does not drop tokens from the curve.
    """
    excess_col = _EXCESS_COL[cohort]
    closed_col = _CLOSED_COL[cohort]
    sql = f"""
        SELECT mr.tweet_ts, mr.{excess_col} AS x
        FROM mention_returns mr
        WHERE mr.account_id = :aid
          AND mr.{closed_col}
          AND mr.{excess_col} IS NOT NULL
        ORDER BY mr.tweet_ts ASC
    """
    rows = (
        await session.execute(text(sql), {"aid": account_id})
    ).mappings().all()

    pts: list[dict] = []
    total = 0.0
    for i, r in enumerate(rows, start=1):
        total += float(r["x"])
        pts.append(
            {
                "ts": r["tweet_ts"].isoformat(),
                "n": i,
                "cum_mean": total / i,
                "last_excess": float(r["x"]),
            }
        )
    return pts


@router.get("/leaderboard/equity-curves")
async def leaderboard_equity_curves(
    cohort: Cohort = Query("30d"),
    limit: int = Query(10, ge=1, le=50),
    min_n: int = Query(MIN_N, ge=1, le=500),
    view: View = Query("scouts"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Top-N accounts (by damped score) and each one's running-mean curve.

    `view` partitions by concentration to match the leaderboard table.
    `min_n` filters out accounts with too few matured calls — a 1-point
    curve carries no visual signal. Default = 5.
    """
    sql = f"""
        WITH {concentration_cte(cohort)}
        SELECT lc.account_id, lc.handle, lc.display_name,
               lc.n_closed, lc.median_excess
        FROM account_leaderboard_cohort lc
        JOIN concentration c ON c.account_id = lc.account_id
        WHERE lc.cohort = :cohort
          AND lc.median_excess IS NOT NULL
          AND lc.n_closed >= :min_n
          {view_filter_sql(view)}
        ORDER BY lc.median_excess * sqrt(lc.n_closed::float / (lc.n_closed + 5)) DESC NULLS LAST
        LIMIT :limit
    """
    params: dict = {"cohort": cohort, "limit": limit, "min_n": min_n}
    if view != "all":
        params["threshold"] = CONCENTRATION_THRESHOLD
    rows = (await session.execute(text(sql), params)).mappings().all()

    accounts = []
    for r in rows:
        curve = await _running_mean_curve(session, r["account_id"], cohort)
        if not curve:
            continue
        accounts.append(
            {
                "account_id": r["account_id"],
                "handle": r["handle"],
                "display_name": r["display_name"],
                "n_matured": int(r["n_closed"] or 0),
                "median_excess": _f(r["median_excess"]),
                "curve": curve,
            }
        )

    return {
        "cohort": cohort,
        "view": view,
        "accounts": accounts,
    }


@router.get("/account/{handle}/equity-curve")
async def account_equity_curve(
    handle: str,
    cohort: Cohort = Query("30d"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    handle = handle.lstrip("@").lower()
    account = (
        await session.execute(
            text("SELECT id, handle FROM accounts WHERE lower(handle) = :h"),
            {"h": handle},
        )
    ).mappings().first()
    if not account:
        raise HTTPException(status_code=404, detail=f"account @{handle} not found")
    return {
        "handle": account["handle"],
        "cohort": cohort,
        "curve": await _running_mean_curve(session, account["id"], cohort),
    }


@router.get("/leaderboard/token-charts")
async def leaderboard_token_charts(
    cohort: Literal["30d", "90d"] = Query("30d"),
    limit: int = Query(9, ge=1, le=12),
    accounts_limit: int = Query(10, ge=1, le=20),
    min_n: int = Query(MIN_N, ge=1, le=500),
    view: View = Query("scouts"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Top-N tokens (by BTC-excess return over `cohort` from first tracked
    mention) + the mentions made by ALL tracked accounts on each token,
    flagged with `is_top` for the top-`accounts_limit` leaderboard accounts.

    Visual goal: a small-multiples grid where each panel is one token's
    indexed price line plus dots for which tracked account called it and
    when. Top-leaderboard accounts get coloured dots, other tracked
    accounts get greyed dots — so the "who got there first" story stays
    intact even when the day-0 caller isn't in the current top-N (e.g.
    AskVenice on VVV at 90d).
    """
    horizon = _HORIZON_DAYS[cohort]

    # 1. Top leaderboard accounts for this cohort (for colouring + legend).
    # Honours `view` so the coloured dots match the active leaderboard tab.
    top_acc_sql = f"""
        WITH {concentration_cte(cohort)}
        SELECT lc.account_id, lc.handle, lc.display_name,
               lc.n_closed, lc.median_excess
        FROM account_leaderboard_cohort lc
        JOIN concentration c ON c.account_id = lc.account_id
        WHERE lc.cohort = :cohort
          AND lc.median_excess IS NOT NULL
          AND lc.n_closed >= :min_n
          {view_filter_sql(view)}
        ORDER BY lc.median_excess * sqrt(lc.n_closed::float / (lc.n_closed + 5)) DESC NULLS LAST
        LIMIT :alimit
    """
    top_acc_params: dict = {"cohort": cohort, "min_n": min_n, "alimit": accounts_limit}
    if view != "all":
        top_acc_params["threshold"] = CONCENTRATION_THRESHOLD
    top_acc_rows = (
        await session.execute(text(top_acc_sql), top_acc_params)
    ).mappings().all()
    top_acc_ids = {int(r["account_id"]) for r in top_acc_rows}
    accounts_out = [
        {
            "account_id": r["account_id"],
            "handle": r["handle"],
            "display_name": r["display_name"],
            "n_matured": int(r["n_closed"] or 0),
            "median_excess": _f(r["median_excess"]),
        }
        for r in top_acc_rows
    ]

    # 2. Candidate tokens: t0 = first mention BY ANY TRACKED account, so the
    # chart's day-0 always reflects the actual first call (not gated on
    # whether the first caller made the current top-N). Pull token + BTC
    # prices at t0 and t0+horizon to compute BTC-excess return.
    candidate_rows = (
        await session.execute(
            text(
                f"""
                WITH first_mention AS (
                  SELECT token_id, min(tweet_ts) AS t0
                  FROM mentions
                  WHERE token_id IS NOT NULL
                  GROUP BY token_id
                )
                SELECT fm.token_id, fm.t0,
                       t.symbol, t.name, t.coingecko_id,
                       (
                         SELECT close_usd FROM token_prices
                         WHERE token_id = fm.token_id AND granularity='daily'
                           AND ts BETWEEN fm.t0 - INTERVAL '2 days'
                                      AND fm.t0 + INTERVAL '2 days'
                         ORDER BY abs(extract(epoch FROM ts - fm.t0)) ASC
                         LIMIT 1
                       ) AS p_t0,
                       (
                         SELECT close_usd FROM token_prices
                         WHERE token_id = fm.token_id AND granularity='daily'
                           AND ts BETWEEN fm.t0 + INTERVAL '{horizon} days' - INTERVAL '2 days'
                                      AND fm.t0 + INTERVAL '{horizon} days' + INTERVAL '2 days'
                         ORDER BY abs(extract(epoch FROM ts - (fm.t0 + INTERVAL '{horizon} days'))) ASC
                         LIMIT 1
                       ) AS p_end,
                       (
                         SELECT close_usd FROM benchmark_prices
                         WHERE symbol='BTC' AND ts <= fm.t0
                         ORDER BY ts DESC LIMIT 1
                       ) AS btc_t0,
                       (
                         SELECT close_usd FROM benchmark_prices
                         WHERE symbol='BTC' AND ts <= fm.t0 + INTERVAL '{horizon} days'
                         ORDER BY ts DESC LIMIT 1
                       ) AS btc_end
                FROM first_mention fm
                JOIN tokens t ON t.id = fm.token_id
                WHERE fm.t0 + INTERVAL '{horizon} days' < now()
                """
            ),
            {},
        )
    ).mappings().all()

    ranked = []
    for r in candidate_rows:
        if r["p_t0"] is None or r["p_end"] is None:
            continue
        if r["btc_t0"] is None or r["btc_end"] is None:
            continue
        sym = (r["symbol"] or "").upper()
        if sym in _STABLECOIN_SYMBOLS:
            continue
        # Don't show BTC itself — by definition 0% excess.
        if sym == "BTC":
            continue
        p0 = float(r["p_t0"])
        pe = float(r["p_end"])
        btc0 = float(r["btc_t0"])
        btce = float(r["btc_end"])
        if p0 <= 0 or btc0 <= 0:
            continue
        token_ret = pe / p0 - 1.0
        btc_ret = btce / btc0 - 1.0
        excess = token_ret - btc_ret
        ranked.append(
            {
                "row": r,
                "p0": p0,
                "p_end": pe,
                "ret": token_ret,
                "excess": excess,
            }
        )
    ranked.sort(key=lambda x: x["excess"], reverse=True)
    chosen = ranked[:limit]
    if not chosen:
        return {
            "cohort": cohort,
            "horizon_days": horizon,
            "accounts": accounts_out,
            "tokens": [],
        }

    # 3. For each chosen token, daily price series and ALL tracked-account
    # mentions (top-N flagged for colouring).
    tokens_out: list[dict] = []

    for c in chosen:
        r = c["row"]
        token_id = r["token_id"]
        t0 = r["t0"]
        p0 = c["p0"]

        # Daily prices over the [t0, t0 + horizon] window.
        price_rows = (
            await session.execute(
                text(
                    """
                    SELECT ts, close_usd FROM token_prices
                    WHERE token_id = :tid AND granularity='daily'
                      AND ts BETWEEN :start AND :end
                    ORDER BY ts ASC
                    """
                ),
                {
                    "tid": token_id,
                    "start": t0 - timedelta(days=1),
                    "end": t0 + timedelta(days=horizon + 1),
                },
            )
        ).mappings().all()
        series: list[dict] = [{"day": 0.0, "indexed": 1.0}]
        for pr in price_rows:
            day = (pr["ts"] - t0).total_seconds() / 86400.0
            if day < 0 or day > horizon + 0.5:
                continue
            series.append(
                {
                    "day": round(day, 2),
                    "indexed": float(pr["close_usd"]) / p0,
                }
            )

        # ALL tracked-account mentions inside [t0, t0 + horizon]. The `mentions`
        # table only contains tweets from accounts we watch, so no extra filter
        # is needed. `is_top` flags the current top-N (coloured); others render
        # greyed so the day-0 caller stays visible even when not in top-N.
        # LEFT JOIN raw_tweets so we can ship the cached oEmbed HTML alongside
        # each dot — frontend hands it to widgets.js for a real branded card
        # on hover, with the plain tweet_text as instant fallback. raw_tweets
        # row is keyed on tweet_id and shared across mentions of the same
        # tweet (one tweet can resolve to multiple tokens).
        mention_rows = (
            await session.execute(
                text(
                    """
                    SELECT m.account_id, a.handle, m.tweet_ts, m.price_at_mention,
                           m.tweet_id, m.tweet_text,
                           rt.oembed_html, rt.oembed_error
                    FROM mentions m
                    JOIN accounts a ON a.id = m.account_id
                    LEFT JOIN raw_tweets rt ON rt.tweet_id = m.tweet_id
                    WHERE m.token_id = :tid
                      AND m.tweet_ts BETWEEN :start AND :end
                    ORDER BY m.tweet_ts ASC
                    """
                ),
                {
                    "tid": token_id,
                    "start": t0,
                    "end": t0 + timedelta(days=horizon),
                },
            )
        ).mappings().all()
        mentions_out: list[dict] = []
        for m in mention_rows:
            day = (m["tweet_ts"] - t0).total_seconds() / 86400.0
            mp = float(m["price_at_mention"]) if m["price_at_mention"] is not None else None
            captured = (c["p_end"] / mp - 1.0) if mp and mp > 0 else None
            mentions_out.append(
                {
                    "handle": m["handle"],
                    "is_top": int(m["account_id"]) in top_acc_ids,
                    "day": round(day, 2),
                    "indexed": (mp / p0) if mp and mp > 0 else None,
                    "captured_ret": captured,
                    "tweet_ts": m["tweet_ts"].isoformat(),
                    "tweet_id": m["tweet_id"],
                    "tweet_text": m["tweet_text"],
                    "oembed_html": m["oembed_html"],
                    # `oembed_error` non-null = X told us this tweet can't
                    # embed (deleted/private/forbidden). Frontend uses it to
                    # decide between iframe attempt vs plain-text card.
                    "oembed_error": m["oembed_error"],
                }
            )

        tokens_out.append(
            {
                "token_id": token_id,
                "symbol": r["symbol"],
                "name": r["name"],
                "coingecko_id": r["coingecko_id"],
                "t0_ts": t0.isoformat(),
                "p0": p0,
                "p_end": c["p_end"],
                "total_return": c["ret"],
                "excess_return": c["excess"],
                "series": series,
                "mentions": mentions_out,
            }
        )

    return {
        "cohort": cohort,
        "horizon_days": horizon,
        "accounts": accounts_out,
        "tokens": tokens_out,
    }


@router.get("/account/{handle}/mention-curves")
async def account_mention_curves(
    handle: str,
    cohort: Cohort = Query("30d"),
    limit: int = Query(80, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """For each matured call in the cohort, a daily BTC-excess series from
    t0 → t0 + horizon. Anchored at (0, 0).
    """
    handle_lc = handle.lstrip("@").lower()
    account = (
        await session.execute(
            text("SELECT id, handle FROM accounts WHERE lower(handle) = :h"),
            {"h": handle_lc},
        )
    ).mappings().first()
    if not account:
        raise HTTPException(status_code=404, detail=f"account @{handle} not found")

    horizon = _HORIZON_DAYS[cohort]
    closed_col = _CLOSED_COL[cohort]
    excess_col = _EXCESS_COL[cohort]

    mentions = (
        await session.execute(
            text(
                f"""
                SELECT m.id, m.token_id, m.tweet_ts, m.price_at_mention,
                       t.symbol, t.coingecko_id,
                       mr.{excess_col} AS final_excess,
                       mr.r_30d, mr.r_90d, mr.r_365d
                FROM mentions m
                LEFT JOIN tokens t ON t.id = m.token_id
                LEFT JOIN mention_returns mr ON mr.id = m.id
                WHERE m.account_id = :aid
                  AND mr.{closed_col}
                  AND mr.{excess_col} IS NOT NULL
                  AND m.token_id IS NOT NULL
                  AND m.price_at_mention IS NOT NULL
                ORDER BY m.tweet_ts DESC
                LIMIT :limit
                """
            ),
            {"aid": account["id"], "limit": limit},
        )
    ).mappings().all()

    if not mentions:
        return {"handle": account["handle"], "cohort": cohort, "horizon_days": horizon, "mentions": []}

    # Pull BTC daily anchor + horizon-day range in one shot, then group in Python.
    earliest = min(m["tweet_ts"] for m in mentions)
    latest = max(m["tweet_ts"] for m in mentions) + timedelta(days=horizon + 2)
    btc_rows = (
        await session.execute(
            text(
                """
                SELECT ts, close_usd FROM benchmark_prices
                WHERE symbol='BTC' AND ts BETWEEN :start AND :end
                ORDER BY ts ASC
                """
            ),
            {"start": earliest - timedelta(days=2), "end": latest},
        )
    ).mappings().all()
    btc_by_date = {r["ts"].date(): float(r["close_usd"]) for r in btc_rows}

    def _btc_at(d):
        """Last available BTC close on-or-before date d."""
        for back in range(0, 8):
            v = btc_by_date.get(d - timedelta(days=back))
            if v is not None:
                return v
        return None

    out_mentions: list[dict] = []
    for m in mentions:
        p0 = float(m["price_at_mention"])
        if p0 <= 0:
            continue
        t0 = m["tweet_ts"]
        btc_t0 = _btc_at(t0.date())
        if btc_t0 is None or btc_t0 <= 0:
            continue

        # Pull daily prices for this token's window
        rows = (
            await session.execute(
                text(
                    """
                    SELECT ts, close_usd FROM token_prices
                    WHERE token_id = :tid
                      AND granularity = 'daily'
                      AND ts BETWEEN :start AND :end
                    ORDER BY ts ASC
                    """
                ),
                {
                    "tid": m["token_id"],
                    "start": t0,
                    "end": t0 + timedelta(days=horizon + 1),
                },
            )
        ).mappings().all()

        points: list[dict] = []
        # Always include the anchor at day 0.
        points.append({"day": 0.0, "excess": 0.0, "token_ret": 0.0, "btc_ret": 0.0})
        for r in rows:
            ts = r["ts"]
            day = (ts - t0).total_seconds() / 86400.0
            if day <= 0 or day > horizon:
                continue
            btc_now = _btc_at(ts.date())
            if btc_now is None:
                continue
            token_ret = float(r["close_usd"]) / p0 - 1.0
            btc_ret = btc_now / btc_t0 - 1.0
            points.append(
                {
                    "day": round(day, 2),
                    "excess": token_ret - btc_ret,
                    "token_ret": token_ret,
                    "btc_ret": btc_ret,
                }
            )
        if len(points) < 2:
            continue
        out_mentions.append(
            {
                "id": m["id"],
                "tweet_ts": t0.isoformat(),
                "symbol": m["symbol"],
                "coingecko_id": m["coingecko_id"],
                "final_excess": _f(m["final_excess"]),
                "points": points,
            }
        )

    return {
        "handle": account["handle"],
        "cohort": cohort,
        "horizon_days": horizon,
        "mentions": out_mentions,
    }


@router.get("/account/{handle}/best-call")
async def account_best_call(
    handle: str,
    cohort: Cohort = Query("30d"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Best matured call for one handle in the given cohort, ranked by raw
    cohort-horizon return (r_30d / r_90d / r_365d).

    Used by the podium card on the home page so the "best call this 30d"
    surface is the handle's actual top return — not just whatever happens to
    appear in the curated token-charts panel. Raw (not excess) so the figure
    reads as "the call captured +X%" — matches the magnitude users intuit.
    """
    handle_lc = handle.lstrip("@").lower()
    account = (
        await session.execute(
            text("SELECT id, handle FROM accounts WHERE lower(handle) = :h"),
            {"h": handle_lc},
        )
    ).mappings().first()
    if not account:
        raise HTTPException(status_code=404, detail=f"account @{handle} not found")

    closed_col = _CLOSED_COL[cohort]
    raw_col = _RAW_COL[cohort]
    excess_col = _EXCESS_COL[cohort]

    sql = f"""
        SELECT m.id, m.tweet_ts, t.symbol,
               mr.{raw_col} AS raw_ret,
               mr.{excess_col} AS excess_ret
        FROM mentions m
        JOIN mention_returns mr ON mr.id = m.id
        JOIN tokens t ON t.id = m.token_id
        WHERE m.account_id = :aid
          AND mr.{closed_col}
          AND mr.{raw_col} IS NOT NULL
        ORDER BY mr.{raw_col} DESC
        LIMIT 1
    """
    row = (
        await session.execute(text(sql), {"aid": account["id"]})
    ).mappings().first()

    if not row:
        return {
            "handle": account["handle"],
            "cohort": cohort,
            "best_call": None,
        }

    return {
        "handle": account["handle"],
        "cohort": cohort,
        "best_call": {
            "mention_id": row["id"],
            "symbol": row["symbol"],
            "raw_ret": _f(row["raw_ret"]),
            "excess_ret": _f(row["excess_ret"]),
            "tweet_ts": row["tweet_ts"].isoformat(),
        },
    }

"""arq worker jobs — Phase 1 implementations.

Each job opens its own DB session via `app.db.SessionLocal`. Redis is on
`ctx['redis']` (arq passes its connection through). Tweets are batch-fetched
where possible to keep X API credit consumption bounded.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.clients.coingecko import CoinGeckoRateLimited
from app.clients.twitter import AppOnlyTwitterClient, TwitterClient, refresh_access_token
from app.config import get_settings
from app.db import SessionLocal
from app.models import Account, Mention, RawTweet, Token, User
from app.services import parsing, pricing, resolver

log = logging.getLogger(__name__)

# Server-side filter is `has:cashtags -is:retweet` (see AppOnlyTwitterClient).
# v2 has no min_faves operator, so the engagement floor is applied here on
# the returned `public_metrics.like_count`. Tune in code if signal/cost ratio
# shifts after we have ≥30d of mention-return data.
MIN_LIKES = 50
BATCH_SIZE = 25  # ≤25 from: clauses keeps the query under the 512-char cap.

# ---------- helpers ----------


async def _get_user_with_fresh_token() -> tuple[User, str] | None:
    """Pick the most recently active user and return (user, valid_access_token).

    Phase 1 is single-user; we just take the first user. For Phase 3 the
    caller passes a user_id explicitly.
    """
    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).order_by(User.id.asc()).limit(1))
        ).scalar_one_or_none()
        if not user or not user.twitter_access_token:
            return None

        if (
            user.twitter_token_expires_at
            and user.twitter_token_expires_at <= datetime.now(timezone.utc)
            and user.twitter_refresh_token
        ):
            settings = get_settings()
            tokens = await refresh_access_token(
                settings.twitter_client_id,
                settings.twitter_client_secret,
                user.twitter_refresh_token,
            )
            user.twitter_access_token = tokens["access_token"]
            if tokens.get("refresh_token"):
                user.twitter_refresh_token = tokens["refresh_token"]
            user.twitter_token_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=tokens.get("expires_in", 7200)
            )
            await session.commit()

        return user, user.twitter_access_token


# ---------- jobs ----------


async def sync_user_following(ctx: dict[str, Any]) -> dict[str, Any]:
    """Pull the connected user's follow list, upsert `accounts` rows, and
    enqueue `sync_batch` jobs grouped by `lookback_days` so each batch
    shares a coherent `start_time`.
    """
    snap = await _get_user_with_fresh_token()
    if not snap:
        return {"status": "no_user"}
    _, access_token = snap

    client = TwitterClient(access_token)
    me = (await client.get_me())["data"]
    follows = await client.list_following(me["id"], max_results=2000)

    async with SessionLocal() as session:
        for f in follows:
            existing = (
                await session.execute(
                    select(Account).where(Account.twitter_id == f["id"])
                )
            ).scalar_one_or_none()
            if not existing:
                acct = Account(
                    twitter_id=f["id"],
                    handle=f["username"],
                    display_name=f.get("name"),
                    followers_count=(f.get("public_metrics") or {}).get("followers_count"),
                    lookback_days=90,
                )
                session.add(acct)
                await session.flush()
            else:
                existing.display_name = f.get("name") or existing.display_name
                existing.followers_count = (
                    (f.get("public_metrics") or {}).get("followers_count")
                    or existing.followers_count
                )
        await session.commit()

    enqueued = 0
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(Account.handle, Account.lookback_days))
        ).all()
        # Group by lookback bucket; chunk each bucket into BATCH_SIZE handles.
        by_lookback: dict[int, list[str]] = {}
        for handle, lookback in rows:
            by_lookback.setdefault(lookback or 90, []).append(handle)

        redis = ctx["redis"]
        for lookback, handles in by_lookback.items():
            for i in range(0, len(handles), BATCH_SIZE):
                batch = handles[i : i + BATCH_SIZE]
                await redis.enqueue_job("sync_batch", batch, lookback)
                enqueued += 1

    return {"status": "ok", "follows": len(follows), "batches": enqueued}


async def sync_batch(
    ctx: dict[str, Any],
    handles: list[str],
    lookback_days: int = 90,
) -> dict[str, Any]:
    """PHASE A: fetch tweets from X, persist to raw_tweets, advance cursors.

    Resolution to tokens/mentions happens in `resolve_pending_tweets`. The
    durable boundary at raw_tweets means a CoinGecko outage cannot waste X
    spend — once this commits, the X posts are paid for AND saved.

    One /tweets/search/all call covers up to BATCH_SIZE handles. Tweets
    with `like_count < MIN_LIKES` are dropped here (saves DB write + CG
    quota; X spend is already paid).
    """
    settings = get_settings()
    if not settings.twitter_app_bearer:
        return {"status": "no_app_bearer"}

    client = AppOnlyTwitterClient(settings.twitter_app_bearer)

    async with SessionLocal() as session:
        accts = (
            await session.execute(select(Account).where(Account.handle.in_(handles)))
        ).scalars().all()
        if not accts:
            return {"status": "no_accounts", "handles": handles}

        by_author: dict[str, Account] = {a.twitter_id: a for a in accts}
        # Incremental sync uses the OLDEST last_tweet_id across the batch as
        # since_id so no account is undersampled. Idempotent inserts handle
        # any redundant fetches at the unique tweet_id constraint.
        last_ids = [a.last_tweet_id for a in accts if a.last_tweet_id]
        since_id: str | None = min(last_ids, key=int) if len(last_ids) == len(accts) else None
        start_time: datetime | None = (
            None if since_id else datetime.now(timezone.utc) - timedelta(days=lookback_days)
        )

        # Pre-flight with /tweets/counts/all — free endpoint, skips paid
        # /search/all when the batch has zero matches in the window. Only
        # meaningful in backfill mode (start_time set); incremental sync via
        # since_id has no clean window for counts_all and the spend is small.
        preflight_count: int | None = None
        if start_time is not None:
            try:
                preflight_count = await client.count_kol_calls(
                    [a.handle for a in accts],
                    start_time=start_time,
                    end_time=datetime.now(timezone.utc),
                )
            except httpx.HTTPStatusError as e:
                log.warning(
                    "counts_all pre-flight failed (%s); falling through to search", e
                )

        if preflight_count == 0:
            now = datetime.now(timezone.utc)
            for acct in accts:
                acct.last_synced_at = now
                acct.lookback_days = max(acct.lookback_days or 90, lookback_days)
            await session.commit()
            return {
                "handles": handles,
                "preflight_count": 0,
                "tweets_returned": 0,
                "kept": 0,
                "skipped_low_likes": 0,
                "raw_inserted": 0,
                "resolve_jobs": 0,
            }

        # X fetch — only failure mode is unrecoverable HTTP error; let arq retry
        # the whole batch (no DB writes happened yet, so no waste).
        try:
            tweets = await client.search_kol_calls(
                [a.handle for a in accts], since_id=since_id, start_time=start_time
            )
        except httpx.HTTPStatusError as e:
            log.error("X search failed for batch (%s handles): %s", len(handles), e)
            raise

        kept = 0
        skipped_low_likes = 0
        new_raw_ids: list[int] = []
        per_author_max: dict[str, str] = {}
        per_author_min: dict[str, str] = {}

        for tw in tweets:
            author_id = tw.get("author_id")
            acct = by_author.get(author_id) if author_id else None
            if not acct:
                continue

            tweet_id = tw["id"]
            if author_id not in per_author_max or int(tweet_id) > int(per_author_max[author_id]):
                per_author_max[author_id] = tweet_id
            if author_id not in per_author_min or int(tweet_id) < int(per_author_min[author_id]):
                per_author_min[author_id] = tweet_id

            likes = ((tw.get("public_metrics") or {}).get("like_count")) or 0
            if likes < MIN_LIKES:
                skipped_low_likes += 1
                continue
            kept += 1

            tweet_ts = datetime.fromisoformat(tw["created_at"].replace("Z", "+00:00"))
            text_body = (tw.get("note_tweet") or {}).get("text") or tw.get("text") or ""

            stmt = (
                pg_insert(RawTweet)
                .values(
                    tweet_id=tweet_id,
                    account_id=acct.id,
                    tweet_ts=tweet_ts,
                    tweet_text=text_body,
                    raw_json=tw,
                    fetched_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_nothing(index_elements=["tweet_id"])
                .returning(RawTweet.id)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                new_raw_ids.append(row)

        # Cursor advance — guarded so a stale retry can't move it backwards.
        now = datetime.now(timezone.utc)
        for acct in accts:
            new_max = per_author_max.get(acct.twitter_id)
            new_min = per_author_min.get(acct.twitter_id)
            if new_max and (not acct.last_tweet_id or int(new_max) > int(acct.last_tweet_id)):
                acct.last_tweet_id = new_max
            if new_min and (not acct.oldest_tweet_id or int(new_min) < int(acct.oldest_tweet_id)):
                acct.oldest_tweet_id = new_min
            acct.last_synced_at = now
            acct.lookback_days = max(acct.lookback_days or 90, lookback_days)

        await session.commit()
        account_ids = [a.id for a in accts]

    # Phase B fan-out — chunk so each resolve job stays under job_timeout
    # even if every match hits CG (4s/call → 100 tweets ≤ ~7 min worst case).
    redis = ctx["redis"]
    CHUNK = 100
    resolve_jobs = 0
    for i in range(0, len(new_raw_ids), CHUNK):
        await redis.enqueue_job("resolve_pending_tweets", new_raw_ids[i : i + CHUNK])
        resolve_jobs += 1
    for aid in account_ids:
        await redis.enqueue_job("consider_deepening", aid)

    return {
        "handles": handles,
        "preflight_count": preflight_count,
        "tweets_returned": len(tweets),
        "kept": kept,
        "skipped_low_likes": skipped_low_likes,
        "raw_inserted": len(new_raw_ids),
        "resolve_jobs": resolve_jobs,
    }


async def resolve_pending_tweets(
    ctx: dict[str, Any],
    raw_tweet_ids: list[int],
) -> dict[str, Any]:
    """PHASE B: parse + resolve + write mentions for a chunk of raw_tweets.

    Per-tweet commits — a CoinGecko rate-limit stops the loop cleanly without
    losing work already done in this job. CG outages bubble out as
    `CoinGeckoRateLimited`; the sweeper picks unresolved rows back up.
    Other resolver errors are logged on the row and the loop continues.
    """
    redis = ctx["redis"]
    resolved = 0
    skipped_rate_limited = 0
    new_mention_ids: list[int] = []

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(RawTweet).where(RawTweet.id.in_(raw_tweet_ids))
            )
        ).scalars().all()
        # Detach so we can use them across fresh sessions below.
        snapshots = [
            {
                "id": r.id,
                "tweet_id": r.tweet_id,
                "account_id": r.account_id,
                "tweet_ts": r.tweet_ts,
                "tweet_text": r.tweet_text,
                "raw_json": r.raw_json,
                "resolve_attempts": r.resolve_attempts or 0,
            }
            for r in rows
            if r.resolved_at is None
        ]

    for snap in snapshots:
        try:
            async with SessionLocal() as session:
                matches = parsing.extract_from_tweet(snap["raw_json"])

                tweet_mention_ids: list[int] = []
                for m in matches:
                    token = await resolver.resolve(m, session, redis)
                    if not token:
                        continue
                    stmt = (
                        pg_insert(Mention)
                        .values(
                            account_id=snap["account_id"],
                            tweet_id=snap["tweet_id"],
                            tweet_ts=snap["tweet_ts"],
                            tweet_text=snap["tweet_text"],
                            token_id=token.id,
                            raw_match=m.raw,
                            match_kind=m.kind,
                        )
                        .on_conflict_do_nothing(
                            constraint="u_mention",
                        )
                        .returning(Mention.id)
                    )
                    mid = (await session.execute(stmt)).scalar_one_or_none()
                    if mid is not None:
                        tweet_mention_ids.append(mid)

                # Mark resolved (even if zero matches — that's a valid terminal state).
                await session.execute(
                    text(
                        "UPDATE raw_tweets "
                        "SET resolved_at = now(), "
                        "    resolve_attempts = resolve_attempts + 1, "
                        "    resolve_last_error = NULL "
                        "WHERE id = :rid AND resolved_at IS NULL"
                    ),
                    {"rid": snap["id"]},
                )
                await session.commit()
                resolved += 1
                new_mention_ids.extend(tweet_mention_ids)

        except CoinGeckoRateLimited:
            # CG is throttling hard. Don't bump attempts (we never finished a
            # real try). Stop the loop — sweeper will pick this up later.
            log.warning(
                "CG rate-limited at raw_id=%s — stopping resolve job", snap["id"]
            )
            skipped_rate_limited += 1
            break
        except Exception as e:
            log.exception("resolve failed for raw_id=%s", snap["id"])
            try:
                async with SessionLocal() as fail_session:
                    await fail_session.execute(
                        text(
                            "UPDATE raw_tweets "
                            "SET resolve_attempts = resolve_attempts + 1, "
                            "    resolve_last_error = :err "
                            "WHERE id = :rid"
                        ),
                        {"rid": snap["id"], "err": str(e)[:500]},
                    )
                    await fail_session.commit()
            except Exception:
                log.exception("could not record resolve failure for raw_id=%s", snap["id"])

    for mid in new_mention_ids:
        await redis.enqueue_job("on_new_mention", mid)

    return {
        "input": len(raw_tweet_ids),
        "resolved": resolved,
        "rate_limited": skipped_rate_limited,
        "mentions_inserted": len(new_mention_ids),
    }


async def resolve_pending_sweep(ctx: dict[str, Any]) -> dict[str, Any]:
    """Periodic sweeper: re-enqueue raw_tweets that haven't been resolved yet.

    Skips rows attempted ≥5 times — those need human triage (likely a parsing
    bug or persistent bad data). Gives 5 minutes' grace after fetch so we
    don't double-enqueue tweets the immediate Phase B is already working on.
    """
    redis = ctx["redis"]
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    async with SessionLocal() as session:
        ids = (
            await session.execute(
                select(RawTweet.id)
                .where(
                    RawTweet.resolved_at.is_(None),
                    RawTweet.resolve_attempts < 5,
                    RawTweet.fetched_at < cutoff,
                )
                .order_by(RawTweet.fetched_at.asc())
                .limit(1000)
            )
        ).scalars().all()

    CHUNK = 100
    enqueued = 0
    for i in range(0, len(ids), CHUNK):
        await redis.enqueue_job("resolve_pending_tweets", ids[i : i + CHUNK])
        enqueued += 1
    return {"pending": len(ids), "jobs_enqueued": enqueued}


async def on_new_mention(ctx: dict[str, Any], mention_id: int) -> dict[str, Any]:
    """Fetch the price window and write the anchor."""
    async with SessionLocal() as session:
        mention = (
            await session.execute(select(Mention).where(Mention.id == mention_id))
        ).scalar_one_or_none()
        if not mention or not mention.token_id:
            return {"status": "missing"}
        if mention.price_at_mention is not None:
            return {"status": "already_anchored"}
        token = (
            await session.execute(select(Token).where(Token.id == mention.token_id))
        ).scalar_one_or_none()
        if not token:
            return {"status": "missing_token"}

        await pricing.fetch_and_upsert_anchor(session, token, mention)
        await session.commit()

    return {"mention_id": mention_id, "anchor": "ok"}


async def extend_token_prices_daily(
    ctx: dict[str, Any], token_id: int | None = None
) -> dict[str, Any]:
    """Top up the daily series for one token (`token_id` given) or every active token."""
    added = 0
    async with SessionLocal() as session:
        if token_id is not None:
            tok = (
                await session.execute(select(Token).where(Token.id == token_id))
            ).scalar_one_or_none()
            if tok:
                added += await pricing.extend_daily_series(session, tok)
        else:
            tokens = (await session.execute(select(Token))).scalars().all()
            for tok in tokens:
                added += await pricing.extend_daily_series(session, tok)
        await session.commit()
    return {"rows_added": added}


async def refresh_benchmark_prices(ctx: dict[str, Any]) -> dict[str, Any]:
    async with SessionLocal() as session:
        added = await pricing.refresh_benchmarks(session)
        await session.commit()
    return {"rows_added": added}


async def refresh_mention_returns(ctx: dict[str, Any]) -> dict[str, Any]:
    async with SessionLocal() as session:
        await session.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY mention_returns;"))
        await session.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY account_leaderboard;"))
        await session.commit()
    return {"status": "refreshed"}


async def bootstrap_account_ci(ctx: dict[str, Any]) -> dict[str, Any]:
    """Bootstrap CI on median r_365d_excess per account. 1000 resamples.

    Cheap enough to do nightly even at scale (cap accounts at min N=10).
    """
    import random
    from decimal import Decimal

    rows_written = 0
    async with SessionLocal() as session:
        accounts = (
            await session.execute(
                text(
                    """
                    SELECT account_id, array_agg(r_365d_excess) AS rs
                    FROM mention_returns
                    WHERE is_closed AND r_365d_excess IS NOT NULL
                    GROUP BY account_id
                    HAVING count(*) >= 10
                    """
                )
            )
        ).all()
        for account_id, rs in accounts:
            if not rs:
                continue
            n = len(rs)
            samples: list[float] = []
            for _ in range(1000):
                draw = [rs[random.randrange(n)] for _ in range(n)]
                draw.sort()
                samples.append(float(draw[n // 2]))
            samples.sort()
            lo = samples[25]
            hi = samples[974]
            med = samples[500]
            await session.execute(
                text(
                    """
                    INSERT INTO account_ci (account_id, median_excess, ci_low_excess, ci_high_excess, n_closed, computed_at)
                    VALUES (:aid, :med, :lo, :hi, :n, now())
                    ON CONFLICT (account_id) DO UPDATE
                    SET median_excess=EXCLUDED.median_excess,
                        ci_low_excess=EXCLUDED.ci_low_excess,
                        ci_high_excess=EXCLUDED.ci_high_excess,
                        n_closed=EXCLUDED.n_closed,
                        computed_at=now();
                    """
                ),
                {
                    "aid": account_id,
                    "med": Decimal(str(med)),
                    "lo": Decimal(str(lo)),
                    "hi": Decimal(str(hi)),
                    "n": n,
                },
            )
            rows_written += 1
        await session.commit()
    return {"accounts_updated": rows_written}


async def consider_deepening(ctx: dict[str, Any], account_id: int) -> dict[str, Any]:
    """If the account has ≥5 resolved mentions and lookback < 365d, deepen by 90d."""
    async with SessionLocal() as session:
        acct = (
            await session.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()
        if not acct:
            return {"status": "missing"}
        n_resolved = (
            await session.execute(
                select(func.count())
                .select_from(Mention)
                .where(
                    Mention.account_id == account_id,
                    Mention.price_at_mention.is_not(None),
                )
            )
        ).scalar_one()
        if (acct.lookback_days or 90) >= 365 or n_resolved < 5:
            return {"status": "no_deepen", "n": n_resolved, "lookback": acct.lookback_days}
        new_lookback = (acct.lookback_days or 90) + 90
        acct.lookback_days = new_lookback
        await session.commit()

    redis = ctx["redis"]
    await redis.enqueue_job("sync_batch", [acct.handle], new_lookback)
    return {"status": "deepening", "to": new_lookback}

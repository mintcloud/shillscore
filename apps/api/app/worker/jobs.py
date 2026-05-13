"""arq worker jobs — Phase 1 implementations.

Each job opens its own DB session via `app.db.SessionLocal`. Redis is on
`ctx['redis']` (arq passes its connection through). Tweets are batch-fetched
where possible to keep X API credit consumption bounded.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.clients import coingecko
from app.clients.coingecko import CoinGeckoRateLimited
from app.clients.oembed import TransientOEmbedError, fetch_oembed_html
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

# When set, resolve_pending_tweets skips the per-mention on_new_mention
# enqueue and relies on a follow-up anchor_all_pending sweep for prices.
# Used during bulk re-runs to avoid burning N CG calls when M wide-span
# calls (one per token) would do the job.
SKIP_PER_MENTION_ANCHOR = os.environ.get("SHILLSCORE_SKIP_PER_MENTION_ANCHOR") == "1"

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


async def _register_aliases_from_tweet(
    session,
    account_id: int,
    tweet_id: str,
    raw_json: dict[str, Any],
    token_id: int,
) -> None:
    """After a contract-resolved mention lands, register every $TICKER that
    co-occurred in the same tweet body as an alias from (account, symbol)
    → token_id. Future cashtags from this account routes to this token via
    resolver Tier 3a instead of guessing by mcap rank.
    """
    cashtags = parsing.extract_cashtags_only(raw_json)
    if not cashtags:
        return
    for sym in cashtags:
        await session.execute(
            text(
                """
                INSERT INTO account_token_aliases
                  (account_id, symbol, token_id, last_seen_tweet_id, updated_at)
                VALUES (:aid, :sym, :tid, :twid, now())
                ON CONFLICT (account_id, symbol) DO UPDATE
                  SET token_id = EXCLUDED.token_id,
                      last_seen_tweet_id = EXCLUDED.last_seen_tweet_id,
                      updated_at = now()
                """
            ),
            {"aid": account_id, "sym": sym, "tid": token_id, "twid": tweet_id},
        )


def _gran_for(start: datetime, end: datetime) -> str:
    """CG returns minute / 5min / hourly / daily depending on the span.
    We label the bucket so anchor lookups know which granularity to query."""
    span = end - start
    if span <= timedelta(hours=1):
        return "minute"
    if span <= timedelta(hours=24):
        return "5min"
    if span <= timedelta(days=90):
        return "hourly"
    return "daily"


def _closest(series: list[tuple[datetime, float]], target: datetime) -> tuple[datetime, float]:
    return min(series, key=lambda r: abs((r[0] - target).total_seconds()))


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

    # Warm the oEmbed cache so the next hover lands on a real card, not a
    # plain-text fallback. Cheap (free endpoint) and idempotent.
    if new_raw_ids:
        await redis.enqueue_job("fetch_oembed_pending", min(len(new_raw_ids) * 2, 200))

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

    Three-tier ticker resolution: a `ResolveOutcome` with `token` set → normal
    anchor-bearing mention; with `ambiguous` set → mention stored token_id=NULL
    plus the candidate JSON for later disambiguation (no anchor job enqueued).
    """
    redis = ctx["redis"]
    resolved = 0
    skipped_rate_limited = 0
    new_mention_ids: list[int] = []
    ambiguous_mentions = 0

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
                    outcome = await resolver.resolve(
                        m, session, redis, account_id=snap["account_id"]
                    )

                    if outcome.token is not None:
                        # Normal resolution path — insert mention with token_id.
                        stmt = (
                            pg_insert(Mention)
                            .values(
                                account_id=snap["account_id"],
                                tweet_id=snap["tweet_id"],
                                tweet_ts=snap["tweet_ts"],
                                tweet_text=snap["tweet_text"],
                                token_id=outcome.token.id,
                                raw_match=m.raw,
                                match_kind=m.kind,
                            )
                            .on_conflict_do_nothing(constraint="u_mention")
                            .returning(Mention.id)
                        )
                        mid = (await session.execute(stmt)).scalar_one_or_none()
                        if mid is not None:
                            tweet_mention_ids.append(mid)

                        # Contract-resolved? Register every cashtag in this
                        # tweet as an alias for this token (per-author).
                        if m.kind == "contract":
                            await _register_aliases_from_tweet(
                                session,
                                snap["account_id"],
                                snap["tweet_id"],
                                snap["raw_json"],
                                outcome.token.id,
                            )

                    elif outcome.ambiguous is not None:
                        # Ambiguous symbol — record the candidates, leave
                        # token_id NULL. No anchor job; not counted in
                        # leaderboard (joins on tokens).
                        stmt = pg_insert(Mention).values(
                            account_id=snap["account_id"],
                            tweet_id=snap["tweet_id"],
                            tweet_ts=snap["tweet_ts"],
                            tweet_text=snap["tweet_text"],
                            token_id=None,
                            raw_match=m.raw,
                            match_kind=m.kind,
                            ambiguous_candidates=outcome.ambiguous,
                        )
                        await session.execute(stmt)
                        ambiguous_mentions += 1
                    # else: no match at all — drop silently (as before).

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

    if not SKIP_PER_MENTION_ANCHOR:
        for mid in new_mention_ids:
            await redis.enqueue_job("on_new_mention", mid)

    return {
        "input": len(raw_tweet_ids),
        "resolved": resolved,
        "rate_limited": skipped_rate_limited,
        "mentions_inserted": len(new_mention_ids),
        "ambiguous_inserted": ambiguous_mentions,
        "per_mention_anchor_skipped": SKIP_PER_MENTION_ANCHOR,
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


async def resolve_ambiguous_via_aliases(ctx: dict[str, Any]) -> dict[str, Any]:
    """Retroactive Tier 3a backfill.

    Walks mentions where token_id IS NULL AND ambiguous_candidates IS NOT
    NULL. For each (account_id, raw_match→symbol), check
    account_token_aliases for a matching alias that landed *after* the
    ambiguous mention was created. If found, attach the token and enqueue
    on_new_mention so the anchor job runs.

    Safe to run repeatedly; idempotent on already-resolved rows.
    """
    fixed = 0
    enqueue_ids: list[int] = []
    async with SessionLocal() as session:
        # Pull pending ambiguous mentions joined to any matching alias.
        rows = (
            await session.execute(
                text(
                    """
                    SELECT m.id AS mention_id, a.token_id
                    FROM mentions m
                    JOIN account_token_aliases a
                      ON a.account_id = m.account_id
                     AND a.symbol = upper(regexp_replace(m.raw_match, '^\\$', ''))
                    WHERE m.token_id IS NULL
                      AND m.ambiguous_candidates IS NOT NULL
                    """
                )
            )
        ).all()
        for mention_id, token_id in rows:
            await session.execute(
                text(
                    "UPDATE mentions "
                    "SET token_id = :tid, ambiguous_candidates = NULL "
                    "WHERE id = :mid AND token_id IS NULL"
                ),
                {"tid": token_id, "mid": mention_id},
            )
            enqueue_ids.append(mention_id)
            fixed += 1
        await session.commit()

    redis = ctx["redis"]
    for mid in enqueue_ids:
        await redis.enqueue_job("on_new_mention", mid)

    return {"resolved_via_alias": fixed}


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


async def anchor_all_pending(ctx: dict[str, Any]) -> dict[str, Any]:
    """Bulk anchor sweep — one CG /market_chart/range call per token.

    Groups un-anchored mentions by token, fetches a single wide span that
    brackets every tweet_ts for that token, then slices the series in memory
    to populate each mention's anchor. Idempotent: any mention already
    anchored is skipped by the `WHERE price_at_mention IS NULL` filter, so
    re-running just no-ops.

    Use this after a bulk re-run of resolve_pending_tweets with
    SHILLSCORE_SKIP_PER_MENTION_ANCHOR=1. For live single-tweet ingest, the
    per-mention `on_new_mention` job still does the 5-min slab fresh-anchor.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT m.id, m.token_id, m.tweet_ts, t.coingecko_id
                    FROM mentions m
                    JOIN tokens t ON t.id = m.token_id
                    WHERE m.price_at_mention IS NULL
                      AND t.coingecko_id IS NOT NULL
                    ORDER BY m.token_id, m.tweet_ts
                    """
                )
            )
        ).all()

    by_token: dict[tuple[int, str], list[tuple[int, datetime]]] = {}
    for mid, tid, ts, cg_id in rows:
        by_token.setdefault((tid, cg_id), []).append((mid, ts))

    tokens_processed = 0
    mentions_anchored = 0
    daily_fallbacks = 0
    misses = 0

    for (token_id, cg_id), mentions in by_token.items():
        timestamps = [t for _, t in mentions]
        start = min(timestamps) - timedelta(hours=24)
        end = max(timestamps) + timedelta(hours=24)
        try:
            series = await coingecko.market_chart_range(cg_id, start, end)
        except CoinGeckoRateLimited:
            log.warning(
                "CG rate-limited mid-anchor sweep (token_id=%s); stopping", token_id
            )
            break
        except Exception:
            log.exception("anchor fetch failed for token_id=%s cg_id=%s", token_id, cg_id)
            continue

        anchor_gran = _gran_for(start, end)

        # Daily fallback when the wide-span fetch returned nothing — rare but
        # happens on illiquid / delisted tokens with sparse OHLCV history.
        if not series:
            try:
                series = await coingecko.market_chart_range(
                    cg_id,
                    min(timestamps) - timedelta(days=2),
                    max(timestamps) + timedelta(days=2),
                )
            except Exception:
                log.exception(
                    "anchor daily-fallback failed for token_id=%s", token_id
                )
                series = []
            anchor_gran = "daily-fallback"
            if series:
                daily_fallbacks += 1

        if not series:
            misses += len(mentions)
            continue

        # Single transaction per token: upsert the prices once, then patch
        # every un-anchored mention in this token group.
        async with SessionLocal() as session:
            await pricing._upsert_prices(
                session,
                token_id,
                series,
                "daily" if anchor_gran == "daily-fallback" else anchor_gran,
                "coingecko",
            )
            for mid, mention_ts in mentions:
                anchor_ts, anchor_px = _closest(series, mention_ts)
                await session.execute(
                    text(
                        """
                        UPDATE mentions
                        SET price_at_mention = :px,
                            price_at_mention_ts = :ts,
                            price_anchor_kind = :k,
                            price_source = 'coingecko'
                        WHERE id = :mid AND price_at_mention IS NULL
                        """
                    ),
                    {
                        "px": Decimal(str(anchor_px)),
                        "ts": anchor_ts,
                        "k": anchor_gran,
                        "mid": mid,
                    },
                )
                mentions_anchored += 1
            await session.commit()
        tokens_processed += 1

    return {
        "tokens_processed": tokens_processed,
        "mentions_anchored": mentions_anchored,
        "daily_fallbacks": daily_fallbacks,
        "misses": misses,
        "total_tokens_seen": len(by_token),
    }


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
        await session.execute(
            text("REFRESH MATERIALIZED VIEW CONCURRENTLY account_leaderboard_cohort;")
        )
        await session.commit()
    return {"status": "refreshed"}


async def bootstrap_account_ci(ctx: dict[str, Any]) -> dict[str, Any]:
    """Bootstrap CI on median r_365d_excess per account. 1000 resamples.

    Cheap enough to do nightly even at scale (cap accounts at min N=10).
    """
    import random

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


async def fetch_oembed_pending(
    ctx: dict[str, Any], limit: int = 200
) -> dict[str, Any]:
    """Fetch publish.twitter.com/oEmbed HTML for raw_tweets that don't have
    it cached yet. Free, unauthenticated endpoint — no X API spend.

    Runs every 15 min via cron + at the end of sync_batch to keep new
    tweets warm for the hover-card UI. Picks newest pending tweets first
    so user-visible chart data gets cached before deep backfill.

    Transient failures (rate-limit, 5xx, network) keep the row pending —
    next sweep retries. Terminal failures (404, 403) record the error
    string so we don't keep hammering deleted/private tweets.
    """
    import httpx

    fetched = 0
    terminal_errors = 0
    transient_failures = 0

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT rt.tweet_id, a.handle
                    FROM raw_tweets rt
                    JOIN accounts a ON a.id = rt.account_id
                    WHERE rt.oembed_html IS NULL
                      AND rt.oembed_error IS NULL
                    ORDER BY rt.tweet_ts DESC
                    LIMIT :lim
                    """
                ),
                {"lim": limit},
            )
        ).mappings().all()

    if not rows:
        return {"pending": 0, "fetched": 0, "terminal_errors": 0, "transient_failures": 0}

    # Reuse one httpx client across the batch — connection pool + keep-alive
    # shave latency vs opening per request. Stays well under publish.twitter.com
    # soft per-IP limits at this concurrency (oembed.py caps internal sem=4).
    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=15.0) as client:
        for r in rows:
            tweet_id = r["tweet_id"]
            handle = r["handle"]
            try:
                html, err = await fetch_oembed_html(handle, tweet_id, client=client)
            except TransientOEmbedError as e:
                log.warning("oembed transient for tweet_id=%s: %s", tweet_id, e)
                transient_failures += 1
                continue

            async with SessionLocal() as session:
                await session.execute(
                    text(
                        """
                        UPDATE raw_tweets
                        SET oembed_html = CAST(:html AS text),
                            oembed_fetched_at = CASE WHEN CAST(:html AS text) IS NOT NULL THEN :now ELSE oembed_fetched_at END,
                            oembed_error = CAST(:err AS text)
                        WHERE tweet_id = :tid
                        """
                    ),
                    {"html": html, "err": err, "now": now, "tid": tweet_id},
                )
                await session.commit()

            if html is not None:
                fetched += 1
            else:
                terminal_errors += 1

    return {
        "pending": len(rows),
        "fetched": fetched,
        "terminal_errors": terminal_errors,
        "transient_failures": transient_failures,
    }


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

"""Backfill publish.twitter.com/oEmbed HTML into raw_tweets.oembed_html.

Runs once after migration 0006 to warm the cache for every existing
tweet. Idempotent — skips rows that already have oembed_html or a known
terminal oembed_error.

Run via:
    docker compose -f infra/docker-compose.yml --env-file infra/.env \\
        exec api python -m scripts.backfill_oembed

Env:
    OEMBED_BATCH_SIZE   how many tweets to fetch per outer loop tick (default 200)
    OEMBED_MAX_ROWS     hard cap on total rows processed (default 10000)
    OEMBED_SLEEP_MS     pause between outer batches in ms (default 1500)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from app.clients.oembed import TransientOEmbedError, fetch_oembed_html
from app.db import SessionLocal

log = logging.getLogger("backfill_oembed")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


async def main() -> None:
    batch_size = int(os.environ.get("OEMBED_BATCH_SIZE", "200"))
    max_rows = int(os.environ.get("OEMBED_MAX_ROWS", "10000"))
    sleep_ms = int(os.environ.get("OEMBED_SLEEP_MS", "1500"))

    fetched_total = 0
    terminal_total = 0
    transient_total = 0
    processed_total = 0

    async with httpx.AsyncClient(timeout=20.0) as client:
        while processed_total < max_rows:
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
                        {"lim": batch_size},
                    )
                ).mappings().all()

            if not rows:
                log.info("nothing left to backfill")
                break

            batch_fetched = 0
            batch_terminal = 0
            batch_transient = 0
            now = datetime.now(timezone.utc)

            for r in rows:
                tweet_id = r["tweet_id"]
                handle = r["handle"]
                try:
                    html, err = await fetch_oembed_html(handle, tweet_id, client=client)
                except TransientOEmbedError as e:
                    log.warning("transient on tweet_id=%s: %s", tweet_id, e)
                    batch_transient += 1
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
                    batch_fetched += 1
                else:
                    batch_terminal += 1

            processed_total += len(rows)
            fetched_total += batch_fetched
            terminal_total += batch_terminal
            transient_total += batch_transient
            log.info(
                "batch: fetched=%d terminal=%d transient=%d "
                "(running: fetched=%d terminal=%d transient=%d processed=%d)",
                batch_fetched, batch_terminal, batch_transient,
                fetched_total, terminal_total, transient_total, processed_total,
            )

            if processed_total >= max_rows:
                log.info("hit OEMBED_MAX_ROWS=%d, stopping", max_rows)
                break

            await asyncio.sleep(sleep_ms / 1000.0)

    log.info(
        "done: fetched=%d terminal=%d transient=%d processed=%d",
        fetched_total, terminal_total, transient_total, processed_total,
    )


if __name__ == "__main__":
    asyncio.run(main())

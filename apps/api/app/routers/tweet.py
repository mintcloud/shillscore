"""On-demand tweet oEmbed lookup — fallback when the chart payload didn't
ship cached HTML for a given tweet (e.g. tweet ingested after the last
oEmbed sweep ran, or sweep failed for transient reasons).

Path: GET /api/tweet/{tweet_id}/oembed → {html, error}

Behaviour:
- Cached hit → return immediately (the common case once backfill runs)
- Cached terminal error → return {html: null, error: "<reason>"}
- Not cached → fetch live from publish.twitter.com, persist, return.
  Transient failures bubble as HTTP 503 so the frontend can retry on its
  own schedule.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.oembed import TransientOEmbedError, fetch_oembed_html
from app.db import get_session

router = APIRouter(tags=["tweet"])


@router.get("/tweet/{tweet_id}/oembed")
async def tweet_oembed(
    tweet_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = (
        await session.execute(
            text(
                """
                SELECT rt.tweet_id, rt.oembed_html, rt.oembed_error,
                       a.handle
                FROM raw_tweets rt
                JOIN accounts a ON a.id = rt.account_id
                WHERE rt.tweet_id = :tid
                """
            ),
            {"tid": tweet_id},
        )
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="unknown tweet_id")

    if row["oembed_html"]:
        return {"tweet_id": tweet_id, "html": row["oembed_html"], "error": None}

    if row["oembed_error"]:
        return {"tweet_id": tweet_id, "html": None, "error": row["oembed_error"]}

    # Cache miss — fetch live and persist.
    try:
        html, err = await fetch_oembed_html(row["handle"], tweet_id)
    except TransientOEmbedError as e:
        # Don't poison the row with a transient failure; let it stay
        # pending so the next sweep retries it.
        raise HTTPException(status_code=503, detail=f"oembed transient: {e}") from e

    now = datetime.now(timezone.utc)
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

    return {"tweet_id": tweet_id, "html": html, "error": err}

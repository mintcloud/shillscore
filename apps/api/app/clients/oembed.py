"""publish.twitter.com/oEmbed client — free, unauthenticated, no API tier.

Returns the HTML blockquote + widgets.js script tag that the frontend hands
to platform.twitter.com/widgets.js for a real branded card.

This endpoint is NOT part of the paid X API v2; it sits on publish.twitter.com
and exists to power third-party embeds. Soft IP rate limits only (anecdotally
~300/15min) so we keep concurrency low and store results forever — oEmbed
responses set cache_age=~100 years.

`fetch_oembed_html` is the single entry point used by both the worker
sweeper and the standalone backfill script. Returns either:
- (html, None)            on success
- (None, error_string)    on a terminal failure we don't want to retry
                          ("deleted", "private", "not_found", "forbidden")
- raises TransientOEmbedError on rate-limit / network / 5xx — caller
  decides whether to back off and retry later.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

OEMBED_URL = "https://publish.twitter.com/oembed"
DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=8.0)

# How many concurrent oEmbed requests we'll fire from one process. Keep low
# to stay well under any per-IP soft limit during backfill.
_SEM = asyncio.Semaphore(4)


class TransientOEmbedError(Exception):
    """Retryable: rate-limit, network error, or 5xx. Caller should back off."""


def _build_url(handle: str, tweet_id: str) -> str:
    # The handle is mostly decorative for the URL (X resolves by tweet_id),
    # but we pass the real one so the oEmbed response's author_name etc.
    # come back populated correctly.
    return f"https://twitter.com/{handle}/status/{tweet_id}"


async def fetch_oembed_html(
    handle: str,
    tweet_id: str,
    *,
    theme: str = "dark",
    omit_script: bool = True,
    client: httpx.AsyncClient | None = None,
) -> tuple[str | None, str | None]:
    """Fetch the oEmbed HTML for one tweet.

    `omit_script=True` strips the inline `<script src=widgets.js>` tag from
    the response — we load widgets.js once globally on the frontend, no need
    to ship it inside every cached blockquote.

    Returns (html, None) on success or (None, error) on a terminal failure.
    Raises TransientOEmbedError on retryable issues.
    """
    url = _build_url(handle, tweet_id)
    params = {
        "url": url,
        "theme": theme,
        "omit_script": "true" if omit_script else "false",
        "dnt": "true",  # do-not-track — X's embed flag for "no personalised
                       # tracking pixels"; safer for our users.
        "hide_thread": "false",
        "lang": "en",
    }

    owned = False
    if client is None:
        client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        owned = True

    async with _SEM:
        try:
            r = await client.get(OEMBED_URL, params=params)
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            raise TransientOEmbedError(f"network: {e!r}") from e
        finally:
            if owned:
                await client.aclose()

    if r.status_code == 200:
        try:
            data = r.json()
        except Exception as e:
            return None, f"json_parse: {e!r}"
        html = data.get("html")
        if not html:
            return None, "no_html_in_response"
        return html, None

    if r.status_code == 404:
        # "Not found" from oEmbed = deleted, suspended, or never existed.
        # Terminal — don't retry.
        return None, "not_found"

    if r.status_code == 403:
        # Private / protected account, or geo-blocked. Terminal.
        return None, "forbidden"

    if r.status_code == 401:
        # Sometimes X returns 401 for sensitive content the embed system
        # won't render anon. Terminal — we'll show plain-text fallback.
        return None, "unauthorized"

    if r.status_code == 429 or r.status_code >= 500:
        raise TransientOEmbedError(f"http {r.status_code}: {r.text[:200]}")

    # Any other status — record it and move on, don't retry forever.
    return None, f"http_{r.status_code}"

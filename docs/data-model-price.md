# Price model — canonical

Two stores, separated cleanly. The anchor is denormalized onto every mention; the rest of the price history lives in a mixed-granularity time-series table.

This doc is the canonical version, superseding the earlier hourly-only design. See `plan.md` §3 for the full context.

---

## What we store

### 1. Anchor price, denormalized onto each mention

```sql
ALTER TABLE mentions
  ADD COLUMN price_at_mention      NUMERIC(30,10),
  ADD COLUMN price_at_mention_ts   TIMESTAMPTZ,         -- exact bucket we used
  ADD COLUMN price_anchor_kind     TEXT,                -- '5min' | 'hourly' | 'daily-fallback'
  ADD COLUMN price_source          TEXT;                -- 'coingecko' | 'defillama'
```

Why denormalize: this is *the* anchor of the mention's PnL forever. It never changes. Every downstream query reads it once.

The bucket we snap to depends on how fresh the mention is when we first see it (see priority queue below).

### 2. Mixed-granularity price series per token

```sql
CREATE TABLE token_prices (
  token_id    BIGINT REFERENCES tokens(id),
  ts          TIMESTAMPTZ NOT NULL,
  close_usd   NUMERIC(30,10) NOT NULL,
  granularity TEXT NOT NULL,            -- '5min' | 'hourly' | 'daily'
  source      TEXT,
  PRIMARY KEY (token_id, ts, granularity)
);
CREATE INDEX ON token_prices (token_id, ts DESC);
CREATE INDEX ON token_prices (token_id, granularity, ts);
```

Three populations of rows live here:

- **5-min slabs** around fresh mentions (±2h, written when the mention is first seen and `tweet_ts` is < 23h old — CoinGecko only exposes 5-min data within the last 24h).
- **Hourly slabs** around aged mentions (±24h, written when the priority queue picks them up).
- **Daily continuous series** for every active token, topped up by a daily cron. This is what powers the chart and the PnL math in `mention_returns`.

Storage budget at v1 scale (1k tokens, 1 year, ~30k mentions): ~2M rows total. Trivial.

### 3. Benchmarks (for excess return)

```sql
CREATE TABLE benchmark_prices (
  symbol      TEXT NOT NULL,            -- 'BTC' | 'ETH'
  ts          TIMESTAMPTZ NOT NULL,     -- daily, UTC midnight
  close_usd   NUMERIC(30,10) NOT NULL,
  PRIMARY KEY (symbol, ts)
);
```

We score on `r_365d_excess = r_365d_token − r_365d_BTC` to strip out market regime. Raw return is kept as a UI toggle.

---

## How prices get populated

Four jobs, all in the worker pool.

```
on_new_mention(mention_id):                              # priority-queued
    age = now() - mention.tweet_ts
    if age < 23h:
        window = (tweet_ts - 2h, tweet_ts + 2h)
        gran   = '5min'
    else:
        window = (tweet_ts - 24h, tweet_ts + 24h)
        gran   = 'hourly'
    fetch CoinGecko market_chart_range(token, *window)
    upsert token_prices(... granularity=gran)
    pick the bucket nearest tweet_ts → write back to mention as anchor

extend_token_prices_daily(token_id):                     # daily cron
    last = SELECT MAX(ts) FROM token_prices
           WHERE token_id=$t AND granularity='daily'
    fetch CoinGecko market_chart_range(token, last, now)  # daily resolution
    upsert token_prices(... granularity='daily')

refresh_benchmark_prices():                              # daily cron
    fetch BTC and ETH daily closes since last
    upsert benchmark_prices

backfill_token_history(token_id):                        # one-shot, on first sight
    earliest = SELECT MIN(tweet_ts) FROM mentions WHERE token_id = $t
    fetch CoinGecko market_chart_range(token, earliest, now)
    upsert token_prices(... granularity='daily', source='coingecko')
```

**Priority queue.** `on_new_mention` jobs are queued with `priority = age(tweet_ts)`. The 24h CoinGecko 5-min window is a hard ceiling — we must process fresh mentions before they age out. arq's job priorities (or a separate "fresh" queue) handle this; the worker drains fresh first, then catches up on aged.

**No 5-min back-fill for stale mentions.** The data isn't there. A mention first ingested >24h after its tweet gets the hourly window and a `price_anchor_kind='hourly'` flag. If somehow even the hourly window is unavailable, fall back to the closest daily close and flag `'daily-fallback'`.

---

## How charts read it

A per-mention chart blends granularities:

```sql
SELECT ts, close_usd, granularity
FROM token_prices
WHERE token_id = $token
  AND ts BETWEEN $tweet_ts - INTERVAL '2 hours'
              AND $tweet_ts + INTERVAL '365 days'
ORDER BY ts;
```

Render the high-res slab tightly around `(tweet_ts, price_at_mention)` (vertical anchor marker), daily series afterward. UI default x-axis ends at `min(now(), tweet_ts + 365d)`.

---

## How scoring reads it

PnL math in `mention_returns` (materialized view, refreshed daily) uses **only the daily granularity** for `r_1d` / `r_7d` / `r_30d` / `r_90d` / `r_180d` / `r_365d`. The 5-min/hourly slabs feed a separate small view that produces `r_5min` / `r_15min` / `r_1h` for the freshness-window mentions only — these are surfaced on the account detail page (and feed the "moves price" badge for >100k-follower accounts) but don't enter the leaderboard score.

Leaderboard sorts on `r_365d_excess` by default, with a "raw return" toggle.

---

## Migration notes

Originally proposed: hourly continuous series everywhere. Switched to daily continuous + per-mention slabs because at ≤10 users daily resolution is sufficient for every horizon we score on, and CoinGecko's free tier handles daily comfortably. The schema is timestamp-keyed and granularity-agnostic — bumping daily to hourly later is a non-breaking config change.

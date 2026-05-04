# Price model — redesign

You're right. My original `price_snapshots(token_id, ts)` with four anchor points (t0, +1d, +7d, +30d) gives me enough for a leaderboard number but kills the chart UX. If we want to plot price action from tweet → now, we need a continuous series.

Here's the corrected model.

---

## What we store

Two things, separated cleanly:

### 1. The anchor price, denormalized onto each mention

```sql
ALTER TABLE mentions
  ADD COLUMN price_at_mention      NUMERIC(30,10),
  ADD COLUMN price_at_mention_ts   TIMESTAMPTZ,         -- the exact bucket we used
  ADD COLUMN price_source          TEXT;                -- 'coingecko'|'defillama'
```

Why denormalize: this number is *the* anchor of the mention's PnL forever. It never changes. Storing it on the row makes every downstream query a single read. We snap it to the nearest hourly bucket (within ±30 min of `tweet_ts`) and remember which bucket we used.

### 2. A continuous price series per token

```sql
CREATE TABLE token_prices (
  token_id  BIGINT REFERENCES tokens(id),
  ts        TIMESTAMPTZ NOT NULL,        -- hourly bucket (UTC, minute=0)
  close_usd NUMERIC(30,10) NOT NULL,
  source    TEXT,
  PRIMARY KEY (token_id, ts)
);
CREATE INDEX ON token_prices (token_id, ts DESC);
```

One row per token per hour. Coverage: `[earliest_mention_ts - 1h, now()]` for every token that has at least one mention. We extend forward continuously as time passes; we never expire old rows (storage is a non-issue: ~2M rows for 1000 tokens × 3 months).

This replaces the old `price_snapshots` table entirely.

---

## How prices get populated

Three jobs, all in the worker pool.

```
on_new_mention(mention_id):
    bucket = round_to_hour(mention.tweet_ts)
    if (token_id, bucket) not in token_prices:
        fetch CoinGecko market_chart_range(token, bucket-1h, bucket+1h)
        upsert token_prices
    mention.price_at_mention      = token_prices[bucket].close_usd
    mention.price_at_mention_ts   = bucket
```

```
backfill_token_history(token_id):
    # First time we see this token
    earliest = (SELECT MIN(tweet_ts) FROM mentions WHERE token_id = $t)
    fetch CoinGecko market_chart_range(token, earliest, now)
    upsert token_prices  (resampled to hourly)
```

```
extend_token_prices(token_id):
    # Keep every token current, runs hourly via cron
    last = (SELECT MAX(ts) FROM token_prices WHERE token_id = $t)
    fetch CoinGecko market_chart_range(token, last, now)
    upsert token_prices
```

CoinGecko's `/coins/{id}/market_chart/range` already returns hourly granularity for ranges within 90 days, daily beyond. We resample to hourly buckets (forward-fill if a gap, label `source='coingecko-ff'` so we know).

---

## How the chart works

Per-mention chart is a single query:

```sql
SELECT ts, close_usd
FROM token_prices
WHERE token_id = $token
  AND ts BETWEEN $tweet_ts AND $tweet_ts + INTERVAL '90 days'
ORDER BY ts;
```

Plot it. Mark the anchor point at `(tweet_ts, price_at_mention)` with a vertical line. End of the chart is either +90d or `now()`, whichever comes first — that's your "open window."

For "from the tweet till now" without the 90-day cap, drop the upper bound:

```sql
WHERE token_id = $token AND ts >= $tweet_ts
```

Which view to show is a UI toggle — I'd default to "min(now, +90d)" so the leaderboard math and the chart agree.

---

## How PnL / signal scoring works now

Computed on the fly from the series, no pre-canned snapshot columns:

```sql
-- For one mention
WITH anchor AS (
  SELECT price_at_mention AS p0, tweet_ts FROM mentions WHERE id = $m
),
horizons AS (
  SELECT
    (SELECT close_usd FROM token_prices
     WHERE token_id = $t AND ts <= a.tweet_ts + INTERVAL '1 day'
     ORDER BY ts DESC LIMIT 1) AS p_1d,
    (SELECT close_usd FROM token_prices
     WHERE token_id = $t AND ts <= a.tweet_ts + INTERVAL '7 days'
     ORDER BY ts DESC LIMIT 1) AS p_7d,
    (SELECT close_usd FROM token_prices
     WHERE token_id = $t AND ts <= a.tweet_ts + INTERVAL '30 days'
     ORDER BY ts DESC LIMIT 1) AS p_30d,
    (SELECT close_usd FROM token_prices
     WHERE token_id = $t AND ts <= a.tweet_ts + INTERVAL '90 days'
     ORDER BY ts DESC LIMIT 1) AS p_90d,
    (SELECT close_usd FROM token_prices
     WHERE token_id = $t ORDER BY ts DESC LIMIT 1)            AS p_now
  FROM anchor a
)
SELECT (p_1d - p0)/p0 r_1d, (p_7d - p0)/p0 r_7d,
       (p_30d - p0)/p0 r_30d, (p_90d - p0)/p0 r_90d,
       (p_now - p0)/p0 r_open
FROM anchor, horizons;
```

For the leaderboard we materialize this into a view that refreshes hourly:

```sql
CREATE MATERIALIZED VIEW mention_returns AS
SELECT m.id, m.account_id, m.token_id, m.tweet_ts, m.price_at_mention,
       <the horizon math above>,
       (m.tweet_ts + INTERVAL '90 days' < now()) AS is_closed
FROM mentions m;
```

A mention is **closed** when 90 days have passed since `tweet_ts`. The leaderboard ranks on `r_90d` over closed mentions only — that's the only honest "did this account pick winners?" number. We separately show `r_open` for mentions still inside their window, labelled "still open" in the UI so people don't conflate them.

---

## What changes vs the original plan

| Thing | Before | After |
|---|---|---|
| Table for prices | `price_snapshots(token_id, ts)`, 4 rows per mention | `token_prices(token_id, ts)`, hourly continuous |
| Anchor price | One row in `price_snapshots` with `ts == tweet_ts` | Denormalized onto `mentions.price_at_mention` |
| Chart support | Not really — only 4 dots | Native — query the series |
| PnL columns | Pre-stored on snapshots | Computed in materialized view from series |
| 90-day window | Implicit | Explicit `is_closed` flag, drives leaderboard |
| Storage | Tiny | ~2M rows per 1000 tokens — still tiny |

Section 3 (data model) and section 6 (signal scoring) of the original plan are superseded by this doc. Everything else stands.

---

## Two small calls baked in (overridable)

1. **Hourly granularity, not 5-min.** CoinGecko gives 5-min data only within the last day. Backfilling old mentions caps you at hourly anyway, so consistency wins. 5-min wouldn't change leaderboard scoring meaningfully.
2. **No "high/low" bands stored.** Just close. If you want "max drawdown during the window" later, we can add `high`/`low` to `token_prices` — non-breaking change.

If you want either flipped, say so before I scaffold.

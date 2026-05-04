# shillscore — Build Plan (v3, canonical)

A public, open-source dashboard that turns crypto-Twitter into a backtested feed. Connect Twitter → parse token mentions across followed accounts → snapshot price at mention time → rank accounts by signal quality. Network effect: once an account is parsed for one user, every later user only triggers a *diff* fetch.

This document is canonical. It supersedes the prior v1 and v2 planning drafts by folding in the granularity/window decision (`20260504-173204-f6215c`) and the fidelity/bias decision (`20260504-174328-d731fb`). The brief Vercel/Supabase detour and the pivot-analysis doc are not part of the canonical record — stack stayed Hetzner + FastAPI + Postgres + Redis + arq + Next.js throughout.

---

## 0. Decisions log

Resolved in conversation:

- **Build target.** Idea #3 from the side-projects brief (`20260504-083930-000dbd`).
- **Stack.** Hetzner VPS + FastAPI (Python 3.12) + Postgres 16 + Redis 7 + arq + Next.js 15. All in one `docker-compose.yaml` behind the existing Cloudflare tunnel. Next.js is **self-hosted on the VPS**, not on Vercel.
- **Network-effect backend.** Global `accounts` + `mentions` tables; new users diff-fetch from a stored cursor. `user_follows` is a junction table — never exposed publicly.
- **Price source v1: CoinGecko.** 0x rejected (depth-dependent, spot-only, no historical).
- **Continuous price series: daily, not hourly.** At ≤10 users daily resolution is plenty for every horizon we score on. Switching back to hourly is non-breaking (timestamp-keyed schema doesn't care).
- **Anchor price: per-mention high-res window via priority queue.**
  - **Fresh** (`now − tweet_ts < 23h`): ±2h at **5-min** resolution (~30 points). CoinGecko gives 5-min only within last 24h, so we must catch these in time.
  - **Aged** (`> 23h`): ±24h at **hourly** resolution (~25 points).
  - These rows live in `token_prices` with a `granularity` column; we do not back-fill 5-min for stale mentions (the data isn't there).
- **PnL window: 90d → 365d.** Storage is ~30 MB; CoinGecko free tier handles it. `is_closed` flips at 365d. Add `r_180d` and `r_365d` to the materialized view.
- **Tweet lookback: adaptive deepening, not flat 365d.** X API credits are the wall — every redundant fetch is real money. Ship 90d initial; deepen to 180d / 270d / 365d only for accounts that hit ≥5 resolved mentions. The boring ~80% stop at 90d. (For reference: 5K accts × 365d × 3 tw/d ≈ 5.5M tweet pulls if you went flat — adaptive cuts that by an order of magnitude.)
- **Macro/regime bias: excess return is primary, raw is secondary.** New `benchmark_prices` table (BTC, ETH daily). `r_365d_excess = r_365d_token − r_365d_BTC`. Leaderboard sorts on excess by default; raw is a toggle.
- **Sample-size asymmetry: fixed shared lookback for ranking.** Adaptive deepening enriches the account detail page but *the leaderboard score uses a fixed 365d window*. Decouples "how much we know" from "what we rank on."
- **Survivorship within the deepened set: leave it.** Self-correcting — extending lookback usually surfaces worse pre-period and *lowers* the score.
- **Min N = 10 closed mentions to be ranked.** Below that, accounts show on detail pages with a "needs more data" badge but don't enter the leaderboard.
- **Cohort tabs on the leaderboard:** 90d / 365d / all-time.
- **Bootstrap CI on median return** (1000 resamples, nightly). Display ±CI on the leaderboard so noisy accounts visibly have wider error bars.
- **Soft polish kept:** sqrt-N weight on the score (`median * sqrt(min(n,100)/100)`) to damp the "two-lucky-picks" effect.
- **`r_5min` / `r_15min` / `r_1h` are v1 free output**, not Phase 5. They drop out of the high-res anchor fetch.
- **Open source from day 1.** Public GH repo, MIT license. GitHub login as secondary auth + profile flair.
- **You seed it.** User #1, you eat the cold-start cost.
- **Privacy stance.** No "user X follows account Y" ever shown publicly. Per-user follow lists are local plumbing, deleted on disconnect.
- **X API access: pay-per-use credits, not a flat-fee tier.** Reuse Theo's existing pay-per-use credit account; mint a fresh OAuth 2.0 app for shillscore (own `client_id` + `client_secret`, tokens stored separately from twitter-digest). Architecture must minimise call volume: batch user/tweet lookups (up to 100 ids/call), persistent cache so we never re-fetch the same tweet, cron cadence tuned to credit budget — not freshness. Failure mode is "spent real money," not "rate-limited."

Still open (defaults noted):

| Question | Default if not answered |
|---|---|
| Repo name | `shillscore` |
| Domain | Subdomain of `theogonella.com` |

---

## 1. Architecture

```
                       ┌──────────────────────────┐
                       │    Public Leaderboard    │  ← read-only, anyone
                       │  (Next.js, ISR hourly)   │
                       └────────────┬─────────────┘
                                    │ /api/leaderboard
                                    │ /api/account/{handle}
                                    │ /api/mention/{id}/series  ← chart
                                    ▼
┌──────────────────┐         ┌──────────────────┐         ┌──────────────────┐
│  Connect Twitter │────────▶│   FastAPI app    │◀────────│  Connect GitHub  │
│   (OAuth 2 PKCE) │         │  (Python 3.12)   │         │  (OAuth, optional)│
└──────────────────┘         └────────┬─────────┘         └──────────────────┘
                                      │
                  ┌───────────────────┼───────────────────┐
                  ▼                   ▼                   ▼
          ┌────────────┐      ┌────────────┐      ┌────────────┐
          │  Postgres  │      │ Worker pool │     │   Redis    │
          │ (mat. view │      │  (arq jobs) │     │ (queue +   │
          │  refreshes)│      │             │     │  rate-lim.)│
          └────────────┘      └─────┬──────┘      └────────────┘
                                    │
                ┌───────────────────┼─────────────────────┐
                ▼                   ▼                     ▼
         Twitter API v2     CoinGecko (prices)     Token resolver
        (tweets, follows)   DefiLlama (fallback)   (CG list + 0x)
```

Three processes (`api`, `worker`, `web`), one `docker-compose.yaml` on Hetzner, behind the existing Cloudflare tunnel.

Worker job catalogue:

- `sync_account(handle, since_id?, lookback_days?)` — fetch tweets, emit mentions.
- `resolve_token(raw_match, context)` — ticker/contract → `tokens` row.
- `on_new_mention(mention_id)` — **priority-queued**: fresh (<23h) gets 5-min ±2h window; aged gets hourly ±24h. Writes window into `token_prices` with `granularity`, sets `mentions.price_at_mention`.
- `extend_token_prices_daily(token_id)` — daily cron, top up the daily series.
- `refresh_benchmark_prices()` — daily cron, BTC + ETH closes.
- `refresh_mention_returns()` — daily, refreshes the materialized view.
- `bootstrap_account_ci()` — nightly, 1000-resample CI on median per account.
- `consider_deepening(account_id)` — checks the ≥5-resolved-mentions threshold, enqueues next 90-day chunk if eligible.

---

## 2. Data model (canonical)

Seven tables. **Mentions and accounts are global** (the public good). Per-user data is one junction table.

```sql
-- Twitter account ever referenced by a connected user
CREATE TABLE accounts (
  id              BIGSERIAL PRIMARY KEY,
  twitter_id      TEXT UNIQUE NOT NULL,
  handle          TEXT UNIQUE NOT NULL,
  display_name    TEXT,
  followers_count INT,
  last_synced_at  TIMESTAMPTZ,
  last_tweet_id   TEXT,                  -- highest tweet id we've fetched
  oldest_tweet_id TEXT,                  -- lowest we've back-filled to
  lookback_days   INT DEFAULT 90,        -- adaptive: 90/180/270/365
  first_seen_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON accounts (last_synced_at);

-- A token we know how to price
CREATE TABLE tokens (
  id            BIGSERIAL PRIMARY KEY,
  coingecko_id  TEXT UNIQUE,
  symbol        TEXT NOT NULL,
  name          TEXT,
  contract_addr TEXT,
  chain         TEXT,
  is_verified   BOOLEAN DEFAULT FALSE
);
CREATE UNIQUE INDEX ON tokens (chain, contract_addr) WHERE contract_addr IS NOT NULL;

-- One token mention inside one tweet, with anchor price denormalized
CREATE TABLE mentions (
  id                    BIGSERIAL PRIMARY KEY,
  account_id            BIGINT REFERENCES accounts(id),
  tweet_id              TEXT NOT NULL,
  tweet_ts              TIMESTAMPTZ NOT NULL,
  tweet_text            TEXT NOT NULL,
  token_id              BIGINT REFERENCES tokens(id),
  raw_match             TEXT,
  match_kind            TEXT,                  -- 'ticker' | 'contract'
  sentiment             TEXT,                  -- 'bullish'|'bearish'|'neutral' (LLM, lazy)
  is_self_quote         BOOLEAN,
  -- anchor price (the t0, never changes)
  price_at_mention      NUMERIC(30,10),
  price_at_mention_ts   TIMESTAMPTZ,           -- bucket we used (5-min or hourly)
  price_anchor_kind     TEXT,                  -- '5min' | 'hourly' | 'daily-fallback'
  price_source          TEXT,                  -- 'coingecko'|'defillama'
  CONSTRAINT u_mention UNIQUE (tweet_id, token_id)
);
CREATE INDEX ON mentions (account_id, tweet_ts DESC);
CREATE INDEX ON mentions (token_id, tweet_ts);

-- Continuous price series per token, mixed granularity
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

-- Benchmark prices for excess-return calc (BTC, ETH at minimum)
CREATE TABLE benchmark_prices (
  symbol      TEXT NOT NULL,            -- 'BTC' | 'ETH'
  ts          TIMESTAMPTZ NOT NULL,     -- daily, UTC midnight
  close_usd   NUMERIC(30,10) NOT NULL,
  PRIMARY KEY (symbol, ts)
);

-- Connected user
CREATE TABLE users (
  id           BIGSERIAL PRIMARY KEY,
  twitter_id   TEXT UNIQUE NOT NULL,
  handle       TEXT NOT NULL,
  github_login TEXT,
  joined_at    TIMESTAMPTZ DEFAULT now(),
  last_sync_at TIMESTAMPTZ
);

-- Per-user follow graph (junction). Only per-user data we keep.
CREATE TABLE user_follows (
  user_id     BIGINT REFERENCES users(id) ON DELETE CASCADE,
  account_id  BIGINT REFERENCES accounts(id),
  PRIMARY KEY (user_id, account_id)
);
CREATE INDEX ON user_follows (account_id);   -- inverse query: popularity
```

`user_follows` rows are never exposed publicly. `ON DELETE CASCADE` so disconnecting a user cleans their links while leaving global tables untouched.

---

## 3. Price model (canonical)

Two stores, separated cleanly.

### 3.1 Anchor price, denormalized onto each mention

`mentions.price_at_mention` + `price_at_mention_ts` + `price_anchor_kind` + `price_source`. **Never changes.** Every PnL calc reads it once.

### 3.2 Mixed-granularity series per token

`token_prices(token_id, ts, granularity, close_usd)`. Three populations:

- **5-min slabs** around fresh mentions only (±2h, written at mention-arrival time within the 24h CoinGecko window).
- **Hourly slabs** around aged mentions (±24h, written when the priority queue picks them up).
- **Daily continuous series** for every active token, top-up via daily cron. This is what powers the chart and the PnL calcs.

Storage budget at v1 scale (1k tokens × 1 year):
- Daily continuous: ~365k rows.
- 5-min slabs: ~30k mentions × 30 points = ~1M rows worst case (most tokens have <100 mentions).
- Hourly slabs: ~30k × 25 points = ~750k rows.

Total ~2M rows. Trivial for Postgres on a Hetzner box.

### 3.3 How prices get populated

```
on_new_mention(mention_id):                              # priority-queued
    age = now() - mention.tweet_ts
    if age < 23h:
        window  = (tweet_ts - 2h, tweet_ts + 2h)
        gran    = '5min'
    else:
        window  = (tweet_ts - 24h, tweet_ts + 24h)
        gran    = 'hourly'
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

consider_deepening(account_id):                          # after every sync
    n = SELECT count(*) FROM mentions
        WHERE account_id=$a AND price_at_mention IS NOT NULL
    if n >= 5 and accounts.lookback_days < 365:
        accounts.lookback_days += 90
        enqueue sync_account(handle, lookback_days=accounts.lookback_days)
```

### 3.4 Charts

Per-mention chart blends granularities: 5-min/hourly slab around the anchor, daily afterward.

```sql
SELECT ts, close_usd, granularity
FROM token_prices
WHERE token_id = $token
  AND ts BETWEEN $tweet_ts - INTERVAL '2 hours'
              AND $tweet_ts + INTERVAL '365 days'
ORDER BY ts;
```

Render with the high-res slab around `(tweet_ts, price_at_mention)` (vertical marker), daily series afterward. UI default x-axis ends at `min(now(), tweet_ts + 365d)`.

---

## 4. Signal scoring (canonical)

Computed via materialized view on the daily series; refreshed daily.

```sql
CREATE MATERIALIZED VIEW mention_returns AS
WITH price_at AS (
  -- helper: closest daily close at-or-before a given ts
  SELECT m.id, m.account_id, m.token_id, m.tweet_ts,
         m.price_at_mention AS p0,
         (SELECT close_usd FROM token_prices
          WHERE token_id=m.token_id AND granularity='daily'
            AND ts <= m.tweet_ts + INTERVAL '1 day'
          ORDER BY ts DESC LIMIT 1) AS p_1d,
         (SELECT close_usd FROM token_prices
          WHERE token_id=m.token_id AND granularity='daily'
            AND ts <= m.tweet_ts + INTERVAL '7 days'
          ORDER BY ts DESC LIMIT 1) AS p_7d,
         (SELECT close_usd FROM token_prices
          WHERE token_id=m.token_id AND granularity='daily'
            AND ts <= m.tweet_ts + INTERVAL '30 days'
          ORDER BY ts DESC LIMIT 1) AS p_30d,
         (SELECT close_usd FROM token_prices
          WHERE token_id=m.token_id AND granularity='daily'
            AND ts <= m.tweet_ts + INTERVAL '90 days'
          ORDER BY ts DESC LIMIT 1) AS p_90d,
         (SELECT close_usd FROM token_prices
          WHERE token_id=m.token_id AND granularity='daily'
            AND ts <= m.tweet_ts + INTERVAL '180 days'
          ORDER BY ts DESC LIMIT 1) AS p_180d,
         (SELECT close_usd FROM token_prices
          WHERE token_id=m.token_id AND granularity='daily'
            AND ts <= m.tweet_ts + INTERVAL '365 days'
          ORDER BY ts DESC LIMIT 1) AS p_365d,
         (SELECT close_usd FROM benchmark_prices
          WHERE symbol='BTC'
            AND ts <= m.tweet_ts + INTERVAL '365 days'
          ORDER BY ts DESC LIMIT 1) AS p_btc_365d,
         (SELECT close_usd FROM benchmark_prices
          WHERE symbol='BTC' AND ts <= m.tweet_ts
          ORDER BY ts DESC LIMIT 1) AS p_btc_t0
  FROM mentions m
  WHERE m.price_at_mention IS NOT NULL
)
SELECT id, account_id, token_id, tweet_ts, p0,
       (p_1d   - p0)/p0 AS r_1d,
       (p_7d   - p0)/p0 AS r_7d,
       (p_30d  - p0)/p0 AS r_30d,
       (p_90d  - p0)/p0 AS r_90d,
       (p_180d - p0)/p0 AS r_180d,
       (p_365d - p0)/p0 AS r_365d,
       (p_365d - p0)/p0 - (p_btc_365d - p_btc_t0)/p_btc_t0 AS r_365d_excess,
       (tweet_ts + INTERVAL '365 days' < now()) AS is_closed
FROM price_at;

CREATE UNIQUE INDEX ON mention_returns (id);
CREATE INDEX ON mention_returns (account_id);
```

Plus high-res returns from the slab:

```sql
-- r_5min, r_15min, r_1h pulled directly from the 5-min/hourly slab
-- (separate view, computed only for mentions with a fresh-window slab)
```

### Account leaderboard

Closed mentions only — that's the only honest "did they pick winners?" number.

```sql
CREATE MATERIALIZED VIEW account_leaderboard AS
SELECT a.id, a.handle, a.display_name, a.followers_count,
       count(*)                                  AS n_closed,
       count(*) FILTER (WHERE r_365d_excess > 0) AS n_winners,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r_365d_excess) AS median_excess,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r_365d)         AS median_raw,
       avg(r_365d_excess)                        AS mean_excess
FROM mention_returns mr
JOIN accounts a ON a.id = mr.account_id
WHERE is_closed
GROUP BY a.id
HAVING count(*) >= 10;                           -- min N
```

Score (default sort): `median_excess * sqrt(min(n_closed, 100) / 100)`.

UI:
- Default tab: **365d cohort** sorted on excess return.
- Other tabs: 90d cohort, all-time.
- ±CI from `bootstrap_account_ci` shown as error bars on every row.
- Toggle: "show raw return" swaps `median_excess` → `median_raw`.

In-window mentions (`NOT is_closed`) are surfaced on the *account detail page* with `r_open`, labelled "still open." They never feed the leaderboard.

### Self-fulfilling alpha caveat

For accounts > 100k followers, also show median `r_15min` from the high-res slab — that's mostly the mention's own impact. UI labels accounts where this is large-positive as "moves price."

---

## 5. Network-effect mechanic

When user N connects:

1. Read their follow list from Twitter API → list of handles.
2. Diff against `accounts`:
   - **Hot** (synced < 24h ago) → just `INSERT INTO user_follows`.
   - **Stale** (synced > 24h ago) → enqueue `sync_account(handle, since_id=last_tweet_id)` (cheap diff).
   - **Cold** (never seen) → enqueue `sync_account(handle, since_id=None, lookback_days=90)` (initial back-fill; deepening kicks in only if the account proves itself).
3. Insert `user_follows` rows.

Caps and guardrails:

- **200 cold back-fills per user per day max**, prioritized by follower count.
- **Cursor drift safety:** if `last_synced_at` > 7 days ago, treat as cold even if `last_tweet_id` exists (Twitter `since_id` doesn't reliably work past ~7 days).
- **Adaptive deepening fires per account, not per user.** First user to surface an account pays the 90-day cost; deepening to 180/270/365 is amortized across all users who follow it.

---

## 6. Token mention parsing

Two extraction patterns:

1. **Contract address (HIGH confidence).** Regex `0x[a-fA-F0-9]{40}` (EVM) and base58 ~32-44 chars (Solana). Resolve via CoinGecko `/coins/contract` and 0x token registry.
2. **`$TICKER` (MEDIUM confidence).** Resolution order:
   - If the same tweet has a contract address → use that, ignore the ticker.
   - If ticker is in CoinGecko top-1000 → use that mapping.
   - Multiple matches → mark `unresolved`, exclude from leaderboard, surface in admin queue.

Out of scope for v1: plain English mentions, image OCR, replies-only mentions.

UI surfaces `mentions_resolved / mentions_total` per account so the disambiguation rate is visible.

---

## 7. Phased build plan

Each phase ends with something deployed and shareable.

### Phase 0 — repo bootstrap (day 1, **done**)

- Public GH repo (`shillscore`), MIT license. ✓
- Folders: `apps/api/`, `apps/web/`, `packages/shared/`, `infra/`, `scripts/`, `docs/`. ✓
- README, plan, data-model docs in `docs/`. ✓

Decisions log in §0 above is the canonical record. ADRs are added going forward when a material new decision is made with the user — not written retroactively to justify the current state.

### Phase 1 — single-user MVP, no UI (weekend 1)

- FastAPI: `/auth/twitter`, `/auth/twitter/callback`. Reuse `oauth2_auth.py` from `twitter-digest/`.
- Worker: `sync_account` → `mentions` rows.
- Worker: `on_new_mention` (priority-queued, 5-min vs hourly window).
- Worker: `extend_token_prices_daily`, `refresh_benchmark_prices`.
- Daily cron: `REFRESH MATERIALIZED VIEW mention_returns`, `account_leaderboard`.
- Nightly: `bootstrap_account_ci`.
- CLI: `python -m shillscore seed --user theogonella` runs end-to-end on your follows.
- Acceptance: SQL query against `account_leaderboard` returns sorted accounts with CIs.

### Phase 2 — public leaderboard UI (weekend 2)

- Next.js `/` — global leaderboard, ISR daily. Cohort tabs (365d default). Excess return primary.
- `/account/[handle]` — every mention with the **price chart** (high-res slab + daily afterward, anchor marker).
- `/mention/[id]` — chart + tweet quote + open/closed status.
- No login required.
- Dark theme, Geist or Inter, no decoration.
- Acceptance: stranger sees real data without logging in; charts plot correctly with the 5-min slab visible around fresh mentions.

### Phase 3 — second user + network-effect plumbing (weekend 3)

- "Connect Twitter" button live.
- Diff logic from §5. 200/day back-fill cap.
- `consider_deepening` runs after each sync.
- `/me` — leaderboard filtered via `user_follows`.
- "Connect GitHub" (scope: `read:user`).
- GH icon on user's account card.
- Acceptance: a friend connects; their cold accounts back-fill, their hot accounts skip — observable in worker logs.

### Phase 4 — polish & open-source ergonomics (weekend 4)

- README with this architecture diagram, getting-started, "why I built this."
- GitHub Actions: ruff, eslint, mypy, tsc, tests.
- Docker images on GHCR.
- `make seed-demo` for a de-personalized demo dataset.
- Twitter card / OG image on leaderboard.

### Phase 5 — defensibility & content (later)

- Weekly auto-tweet: "this week's top signal accounts."
- Email digest opt-in.
- DefiLlama integration as fallback for tail tokens not in CG.
- Sentiment classification (Claude, batched nightly).
- Per-token leaderboard ("who called this earliest?").
- "Self-fulfilling alpha" badge from `r_15min`.
- *Maybe* migrate hot tokens back to hourly continuous series if user count grows.

---

## 8. Stack

| Decision | Choice | Why |
|---|---|---|
| Backend | Python 3.12 + FastAPI | Reuse twitter-digest plumbing |
| ORM | SQLAlchemy 2.0 + Alembic | Boring, fine |
| Queue | Redis + arq | Async-native, lighter than Celery; priority queue support |
| DB | Postgres 16 | Materialized views + JSONB |
| Frontend | Next.js 15 (App Router), self-hosted | ISR for leaderboard, runs in same compose file as api |
| Styling | Tailwind, dark theme | Default |
| Auth | Twitter OAuth 2 PKCE primary; GitHub OAuth secondary | No wallets |
| Price source v1 | CoinGecko | Has historical, sufficient |
| Price source v2 | DefiLlama for tail tokens | Optional |
| Hosting | Hetzner VPS + Cloudflare tunnel | Existing infra |
| License | MIT | Friction-free |

---

## 9. Cost & rate-limit budget

| Resource | Cost model | v1 estimate | Notes |
|---|---|---|---|
| X API | Pay-per-use credits (Theo's existing account) | ~$20–60/mo at v1 cadence | Variable, scales with call volume. Batch lookups (up to 100 ids/call), aggressive cache, no polling. Adaptive deepening keeps tweet pulls bounded. |
| CoinGecko | Free 30 calls/min, optional $129/mo Demo | $0 | Free is fine for v1 with caching; daily series + per-mention windows is bounded |
| Hetzner | already paying | already paying | shared with other services |
| Cloudflare | $0 | $0 | tunnel + DNS |
| **Total v1** | | **~$20–60/mo** | all variable, dominated by X credits |

Credits change the failure mode: you don't get rate-limited, you spend real money. Every new ingest path goes through a credit-cost sanity check before merging. If user count grows past ~50 connected users (several thousand accounts under sync), revisit the cost model at that point — credits may stay cheaper than Pro depending on cadence.

---

## 10. Open decisions for you

1. X API access — pay-per-use credits via existing account, fresh OAuth 2.0 app for shillscore. *Settled.*
2. Repo name — keep `shillscore`. *Settled.*
3. Domain — subdomain of `theogonella.com`? *Default: yes.*

Silence = defaults.

---

## 11. Next deliverables when you say go

- Sync v3 plan + new price-model doc into `~/projects/shillscore/docs/` and push to GH.
- Scaffold phases 0 + 1 (FastAPI app, arq workers, Postgres + Redis in compose).
- Working `python -m shillscore seed` runs end-to-end against your Twitter follows on the VPS.
- README with the architecture diagram and the "why I built this" paragraph.
- Telegram message back with the leaderboard SQL output as a screenshot.

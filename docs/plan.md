# Twitter Alpha Lab — Build Plan

A public, open-source dashboard that turns crypto-Twitter into a backtested feed. You connect Twitter → we parse token mentions across the accounts you follow → we snapshot the price at mention time → we rank accounts by signal quality. Network effect: once an account has been parsed for one user, every subsequent user only triggers a *diff* fetch from the last cursor.

---

## 1. Did I understand correctly?

Restating to make sure I'm building the right thing:

- **Auth & input.** User signs in with Twitter (OAuth 2.0 PKCE). We read their *follows* list. No tweets posted, no bookmarks read.
- **Per-account pipeline.** For every followed account, we fetch tweets, parse token mentions ($TICKER and contract addresses), resolve to a CoinGecko ID, snapshot price at the mention timestamp, and re-snapshot at +1d / +7d / +30d.
- **Signal scoring.** Per-account score = aggregated forward-return after their mentions, with a minimum-mentions denominator. Leaderboard is the public artifact.
- **Network effect (your key addition).** Accounts and mentions are stored *globally*, keyed to the account, not the user. When user N connects:
  - Accounts they follow that are *already parsed up to time T* → just fetch tweets since T (a cheap diff).
  - Accounts not yet seen → full back-fill (e.g., last 30 days).
  - Their per-user record is just a list of `(user_id, account_id)` follow links — no duplicate fetching of the same account.
- **Public dashboard.** As more users connect, coverage widens. Leaderboard is browsable by everyone.
- **You seed it.** You're user #1, you bring your follows, you eat the cold-start cost.
- **Open source.** Repo public on your GitHub from day one. "Login with GitHub" as a secondary auth, and your GH profile is shown on your account card.
- **Price source.** CoinGecko for v1 (it has historical OHLC, free tier covers it). 0x is depth-dependent and only spot — explicitly *not* good for historical signal scoring. Revisit for v2 only if useful.

If any of this is wrong, stop here and tell me which line to change.

---

## 2. Architecture at a glance

```
                       ┌──────────────────────────┐
                       │    Public Leaderboard    │  ← read-only, anyone
                       │  (Next.js, static-ish)   │
                       └────────────┬─────────────┘
                                    │ /api/leaderboard
                                    │ /api/account/{handle}
                                    ▼
┌──────────────────┐         ┌──────────────────┐         ┌──────────────────┐
│  Connect Twitter │────────▶│   FastAPI app    │◀────────│  Connect GitHub  │
│   (OAuth 2 PKCE) │         │  (Python 3.12)   │         │  (OAuth, optional)│
└──────────────────┘         └────────┬─────────┘         └──────────────────┘
                                      │
                  ┌───────────────────┼───────────────────┐
                  │                   │                   │
                  ▼                   ▼                   ▼
          ┌────────────┐      ┌────────────┐      ┌────────────┐
          │  Postgres  │      │ Worker pool │     │   Redis    │
          │  (state +  │      │  (RQ / arq) │     │ (queue +   │
          │  mentions) │      │             │     │  rate-lim.)│
          └────────────┘      └─────┬──────┘      └────────────┘
                                    │
                ┌───────────────────┼─────────────────────┐
                ▼                   ▼                     ▼
         Twitter API v2     CoinGecko (prices)     Token resolver
        (tweets, follows)   DefiLlama (fallback)   (CG list + 0x)
```

Three processes:

1. **`api`** — FastAPI HTTP server. Auth, leaderboard endpoints, "connect" flows.
2. **`worker`** — pulls jobs from Redis. Two job types: `sync_account(handle)` and `snapshot_price(token_id, ts)`.
3. **`web`** — Next.js (or just a Vite + React static build). Talks only to `api`.

All three deploy to your Hetzner VPS via `docker-compose`. Behind the existing Cloudflare tunnel. Postgres + Redis as services in the same compose file.

---

## 3. Data model

Six tables. The key invariant: **mentions and accounts are user-agnostic** — they're a public good. Per-user data is a thin layer on top.

```sql
-- A Twitter account we've ever seen referenced by a connected user
CREATE TABLE accounts (
  id              BIGSERIAL PRIMARY KEY,
  twitter_id      TEXT UNIQUE NOT NULL,
  handle          TEXT UNIQUE NOT NULL,
  display_name    TEXT,
  followers_count INT,
  last_synced_at  TIMESTAMPTZ,           -- when we last ran sync_account
  last_tweet_id   TEXT,                  -- highest tweet ID we've fetched
  oldest_tweet_id TEXT,                  -- lowest we've back-filled to
  first_seen_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON accounts (last_synced_at);

-- A token we know how to price
CREATE TABLE tokens (
  id            BIGSERIAL PRIMARY KEY,
  coingecko_id  TEXT UNIQUE,             -- nullable: unknown tokens get queued
  symbol        TEXT NOT NULL,
  name          TEXT,
  contract_addr TEXT,                    -- when known
  chain         TEXT,                    -- ethereum, base, solana, etc
  is_verified   BOOLEAN DEFAULT FALSE    -- in CG top-N, has contract, etc
);
CREATE UNIQUE INDEX ON tokens (chain, contract_addr) WHERE contract_addr IS NOT NULL;

-- One token mention inside one tweet
CREATE TABLE mentions (
  id              BIGSERIAL PRIMARY KEY,
  account_id      BIGINT REFERENCES accounts(id),
  tweet_id        TEXT NOT NULL,
  tweet_ts        TIMESTAMPTZ NOT NULL,
  tweet_text      TEXT NOT NULL,
  token_id        BIGINT REFERENCES tokens(id),
  raw_match       TEXT,                  -- $APE / 0xabcd...
  match_kind      TEXT,                  -- 'ticker' | 'contract'
  sentiment       TEXT,                  -- 'bullish'|'bearish'|'neutral' (LLM, lazy)
  is_self_quote   BOOLEAN,               -- they're quoting their own thread
  CONSTRAINT u_mention UNIQUE (tweet_id, token_id)
);
CREATE INDEX ON mentions (account_id, tweet_ts DESC);

-- Price snapshot at a specific timestamp (windowed: t0, +1d, +7d, +30d)
CREATE TABLE price_snapshots (
  id            BIGSERIAL PRIMARY KEY,
  token_id      BIGINT REFERENCES tokens(id),
  ts            TIMESTAMPTZ NOT NULL,
  usd_price     NUMERIC(30,10),
  source        TEXT,                    -- 'coingecko' | 'defillama'
  CONSTRAINT u_snap UNIQUE (token_id, ts)
);

-- Connected user
CREATE TABLE users (
  id           BIGSERIAL PRIMARY KEY,
  twitter_id   TEXT UNIQUE NOT NULL,
  handle       TEXT NOT NULL,
  github_login TEXT,                     -- nullable
  joined_at    TIMESTAMPTZ DEFAULT now(),
  last_sync_at TIMESTAMPTZ
);

-- The user → account follow graph (per-user data, but read-anonymized)
CREATE TABLE user_follows (
  user_id     BIGINT REFERENCES users(id),
  account_id  BIGINT REFERENCES accounts(id),
  PRIMARY KEY (user_id, account_id)
);
```

**The leaderboard view** is a single SQL query that joins `accounts`, `mentions`, `price_snapshots` and computes a forward-return aggregate. Fast enough for thousands of accounts as a materialized view refreshed every hour.

---

## 4. The network-effect mechanic, spelled out

This is the bit that turns it from a single-player tool into something that gets cheaper per user.

### When user N connects:

1. **Read their follow list** from Twitter API → list of handles.
2. **Diff against `accounts` table.** Three buckets:
   - **Hot** (synced within last 24h) — do nothing, link the user to the account.
   - **Stale** (synced > 24h ago) — enqueue `sync_account(handle, since_id=last_tweet_id)`. This is a *cheap diff* — only tweets newer than what we already have.
   - **Cold** (never seen) — enqueue `sync_account(handle, since_id=None, lookback_days=30)`. Full back-fill.
3. **Insert `user_follows` rows** so this user gets the global leaderboard filtered to their feed.

### Cost asymmetry:

- First user to bring an account = pays the back-fill cost (~30 days of tweets).
- Every later user who follows the same account = pays only the *delta* since the last sync.
- After a few seed users, ~80% of any new user's accounts will be hot.

### Two failure modes to design for:

- **Whale spam.** A user follows 5,000 accounts. Solution: **cap full back-fills at 200 accounts per user per day**, prioritized by follower count. Slow back-fill in the background.
- **Cursor drift.** Twitter API v2's `since_id` doesn't always work past 7 days. Solution: store both `last_tweet_id` and `last_synced_at`; if `last_synced_at` > 7 days ago, treat as cold even if `last_tweet_id` exists.

---

## 5. Token mention parsing — the disambiguation problem

The hard problem isn't fetching tweets, it's "is `$APE` ApeCoin or some random meme on Solana?" v1 cuts the Gordian knot:

**Two extraction patterns, ranked by confidence:**

1. **Contract address (HIGH confidence).** Regex `0x[a-fA-F0-9]{40}` (EVM) and base58 ~32-44 chars on Solana. Look up via 0x token registry / CoinGecko `/coins/contract` endpoint. If found → resolved.
2. **`$TICKER` (MEDIUM confidence).** Resolve via these rules in order:
   - If the tweet *also* has a contract address → use that, ignore the ticker.
   - If ticker is in CoinGecko top-1000 → use that mapping.
   - If ticker has multiple matches in CG → mark `unresolved`, surface in admin queue, exclude from leaderboard.

**Ignored in v1:** plain English mentions ("just bought some doge"), image OCR, replies-only mentions. Easy to add later, not load-bearing.

**Honest caveat baked into the UI:** every account card shows `mentions_resolved / mentions_total` so users see the disambiguation rate.

---

## 6. Signal scoring

Three returns per mention: `r_1d`, `r_7d`, `r_30d`. All computed as `(price_at_t+N - price_at_t0) / price_at_t0`.

**Per-account score** for the leaderboard:

```
score = median(r_7d) over mentions older than 7 days
weighted_score = score * sqrt(min(N_mentions, 100) / 100)
```

The `sqrt` weighting is a cheap hack to avoid a Twitter account with 2 lucky picks topping the leaderboard. Show both raw `score` and `N_mentions` so users can sort.

**Things to surface clearly (avoid being a hype machine):**
- Median, not mean (mean is dominated by one moonshot).
- `mentions_resolved / mentions_total` — disambiguation transparency.
- A "since when" timestamp — newer accounts have less data.
- A separate column for *self-fulfilling alpha* concerns: if an account has > 100k followers, their mention itself moves the price. Flag accounts where the median `r_15min` (price 15 min after the tweet) is large — that's mostly the mention's own impact.

---

## 7. Phased build plan

Following your "iterative, start simple" preference. Each phase ends with something deployed and shareable.

### Phase 0 — repo bootstrap (day 1)

- Public GitHub repo: `theogonella/twitter-alpha-lab`, MIT license.
- Folders: `api/`, `web/`, `worker/`, `infra/` (docker-compose, migrations).
- Reuse `oauth2_auth.py` from `twitter-digest/` as a starting point.
- One-page README explaining the concept.
- Cloudflare tunnel subdomain reserved (e.g. `alphalab.theogonella.com`).

**Acceptance:** `docker-compose up` starts api + worker + postgres + redis locally.

### Phase 1 — single-user MVP, no UI (weekend 1)

- FastAPI: `/auth/twitter`, `/auth/twitter/callback`.
- Worker: `sync_account(handle)` fetches last 30 days of tweets, parses mentions, persists to DB.
- Worker: `snapshot_price(token_id, ts)` calls CoinGecko, writes `price_snapshots`.
- CLI: `python -m alphalab seed --user theogonella` — runs the whole pipeline for your account, no UI.
- Output: a SQL query you run by hand that prints the leaderboard.

**Acceptance:** you run the seed and get a sorted list of your follows by 7d signal.

### Phase 2 — public leaderboard UI (weekend 2)

- Next.js page at `/` — leaderboard of *all* accounts in DB (since you seeded yours, this is your feed).
- Account detail page `/account/[handle]` — every mention, the tweet, the price chart, the return.
- Static-ish: ISR every 1 hour. No login required to view.
- Dark theme, your usual palette. Geist or Inter; no decoration.

**Acceptance:** you can share `alphalab.theogonella.com` and a stranger sees your data without logging in.

### Phase 3 — second user + network-effect plumbing (weekend 3)

- Wire up the "Connect Twitter" button on the live site.
- Implement the diff logic from §4. Cap on full back-fills.
- Per-user feed view: `/me` shows the leaderboard filtered to accounts the logged-in user follows.
- Add the "Connect GitHub" button (just OAuth, no scopes beyond email + login).
- Show a tiny GH icon link on a user's account card.

**Acceptance:** a friend connects their Twitter, their cold accounts back-fill, their hot accounts skip — observable in worker logs.

### Phase 4 — polish & open-source ergonomics (weekend 4)

- README: architecture diagram (the one above), getting-started for self-hosters, "why I built this" paragraph for the career-transition story.
- GitHub Actions: lint (ruff + eslint), type-check (mypy + tsc), tests.
- Docker images on GHCR.
- `make seed-demo` for a demo dataset (de-personalized) so a contributor can run it locally without Twitter API access.
- Twitter card / OG image on the leaderboard page.

**Acceptance:** a stranger can fork the repo and `docker-compose up` to a working local instance.

### Phase 5 — defensibility & content (later)

- Weekly auto-tweet from the project account: "this week's top signal accounts." Distribution.
- Email digest opt-in for connected users.
- 0x integration as a *secondary* price source (only useful for cross-checking thin liquidity, not historical).
- Sentiment classification on tweets (Claude one-shot, batched nightly).
- Per-token leaderboard (which accounts called this token earliest?).

---

## 8. Stack & decisions

| Decision | Choice | Why |
|---|---|---|
| Language (backend) | Python 3.12 | Reuse twitter-digest OAuth + tweepy plumbing |
| Web framework | FastAPI | Async, cheap, fits the worker model |
| ORM | SQLAlchemy 2.0 + Alembic | Boring, fine |
| Job queue | Redis + arq | Lighter than Celery, async-native |
| DB | Postgres 16 | Materialized views + JSONB for tweet payloads |
| Frontend | Next.js 15 (App Router) | ISR for the leaderboard, easy ship |
| Styling | Tailwind, dark theme | Your default |
| Wallet/auth | Twitter OAuth 2 PKCE primary; GitHub OAuth secondary | No wallets needed (no execution) |
| Price source v1 | CoinGecko (free tier, then Demo paid if needed) | Has historical, sufficient |
| Price source v2 | DefiLlama for tail tokens, 0x for spot sanity | Optional |
| Hosting | Hetzner VPS + Cloudflare tunnel | Same as your other services |
| Open source license | MIT | Friction-free |

**Three things I'd flag for your decision before starting:**

1. **Twitter API tier.** Free tier = 1,500 reads/month total. Way too tight. Basic = $200/month, 50k posts/month + user lookups. Required to actually run this. I'd budget $200/mo and call it tuition for the career-transition portfolio piece. *Or* lean into Phase 1 with synthetic data and only pay when you flip to public.
2. **Privacy stance on follows lists.** Storing user follow lists is sensitive. v1 default: do NOT show "user X follows account Y" anywhere public. The leaderboard is global; per-user views are private. Document this in the README.
3. **Self-hosting story.** Do you want one canonical hosted instance + open source for credibility? Or do you want it to be genuinely deployable by a stranger? They lead to different choices around secrets, CG API keys, etc. I'd start with hosted-canonical and make self-hosting "best effort."

---

## 9. GitHub integration — what it actually does

You asked for it specifically. Three uses:

1. **Login with GitHub** (alongside Twitter). User clicks → OAuth → we store `github_login`. Lightweight.
2. **Account card flair.** If a user has connected GitHub, their leaderboard card shows a GH badge linking to their profile. Tiny social proof.
3. **Repo prominence.** Footer of every page: "Open source · github.com/theogonella/twitter-alpha-lab · Star us." Drives stars from people who already like the dashboard. Good for the career-transition narrative.

What it deliberately does *not* do in v1:

- Connect Code Connect mappings, run actions on push, or anything fancy. Save for v2.
- Authenticate against your repo's issues — no need.

OAuth scopes needed: `read:user` only.

---

## 10. Cost & rate-limit budget

Order-of-magnitude sanity check before you commit.

| Resource | Free | Paid v1 | Notes |
|---|---|---|---|
| Twitter API | 1,500 reads/mo | $200/mo Basic = 50k reads | Need Basic |
| CoinGecko | 30 calls/min | $129/mo Demo | Free fine for v1 with caching |
| Hetzner CPX21 | $5/mo | already paying | shared with other services |
| Cloudflare | $0 | $0 | tunnel + DNS |
| Domain | already own | $0 | |
| **Total v1** | | **~$200/mo** | mostly Twitter |

If user count grows past ~50 connected users (i.e., several thousand accounts under sync), bump to Twitter Pro tier ($5k/mo) — at which point this is a real product and you decide whether to monetize, sponsor, or paywall.

---

## 11. Open questions for you

Before I start writing code, three calls I'd like you to make:

1. **Are we paying for Twitter Basic ($200/mo) from day 1, or running on free tier for the first weekend and accepting that it'll choke at the second user?**
2. **Repo name preference.** `twitter-alpha-lab`, `crypto-twitter-receipts`, `signalcheck`, something else?
3. **Hosting domain.** Subdomain of `theogonella.com`, or a dedicated domain? (If dedicated, register before phase 0 so the README links resolve from the start.)

If I don't hear back, my defaults are: pay Basic from day 1; `twitter-alpha-lab`; subdomain of your existing personal domain.

---

## 12. What I'll deliver next when you say go

- A scaffolded repo on your GitHub with phases 0 + 1 wired up.
- A working `python -m alphalab seed` that runs end-to-end against your Twitter follows on the VPS.
- A first draft README with the architecture diagram and the "why I built this" paragraph.
- A Telegram message back with the leaderboard SQL output as a screenshot.

Everything else (UI, public dashboard, second-user network-effect logic) is sequenced into phases 2–4 above.

# shillscore

Accuracy tracker for crypto Twitter calls. Watch the people you follow, see how their picks actually played out, follow the ones with real edge.

> **Status: Phase 2 — public leaderboard UI live at [shillscore.tg-itsavibe.com](https://shillscore.tg-itsavibe.com).** Cohort-parameterized leaderboard (30d / 90d / 365d) with damped median BTC-excess sort, equity-curve chart, "who caught the winners" token-charts grid, per-account mention-curves chart, account & mention detail pages, and a top-3 podium with Follow-on-X CTAs. Multi-user (Phase 3) is the next slab. See [`docs/plan.md`](docs/plan.md).

## What it does

When a Twitter account you follow mentions a token, shillscore captures the price at that moment, then tracks it forward. Over time each account accumulates a track record: hit rate, median excess return vs BTC, time-to-peak, drawdown. You see the leaderboard filtered to *just the accounts you follow*, so it's your network's signal — not a global pump-noise feed.

## Why

Crypto Twitter is full of confident calls. Almost no-one keeps receipts. shillscore keeps receipts.

## Stack

| Layer | Choice |
|---|---|
| Frontend | Next.js 15 (App Router) + Tailwind, dark theme, self-hosted (standalone build), Recharts + custom-SVG panels |
| API | FastAPI 3.12 + SQLAlchemy 2.0 + Alembic |
| Worker | arq on Redis 7 |
| DB | Postgres 16 |
| Price source | CoinGecko (DefiLlama fallback later) |
| Auth | Twitter OAuth 2.0 PKCE primary; GitHub OAuth secondary |
| Hosting | Hetzner VPS, Cloudflare tunnel, all in one `infra/docker-compose.yml` |

## Repo layout

```
apps/web/                  Next.js frontend (leaderboard + account/mention pages)
apps/web/components/       Chart components (equity curve, token grid, podium)
apps/web/lib/              Shared palette + API client
apps/api/                  FastAPI app + arq worker (same image)
apps/api/app/routers/      /leaderboard, /account, /mention, /leaderboard/{equity-curves,token-charts}
apps/api/migrations/       Alembic migrations
apps/api/scripts/          Operational scripts (e.g. prime_tokens.py)
infra/docker-compose.yml   Stack: postgres, redis, api, worker, web
infra/.env.example         Copy to infra/.env
infra/cloudflare-tunnel.md Tunnel hand-off notes
packages/shared/           (reserved for cross-runtime types)
scripts/                   Operational scripts
docs/                      Plan + data-model docs
```

## Running

```bash
cp infra/.env.example infra/.env   # fill in TWITTER_*, generate POSTGRES_PASSWORD + SESSION_SECRET
make build
make up
make migrate          # 0001 schema, 0002 views/OAuth, 0003 raw_tweets, 0004 cohort_leaderboard, 0005 alias_and_ambiguous
curl http://localhost:3006/                                    # web
docker compose -f infra/docker-compose.yml exec api \
  curl -s http://localhost:8000/health
```

The web container binds to `127.0.0.1:3006` so the Cloudflare tunnel can route `shillscore.tg-itsavibe.com` to it. See [`infra/cloudflare-tunnel.md`](infra/cloudflare-tunnel.md) for the tunnel ingress entry. Both `/api/*` and `/auth/*` are proxied from web → api over the internal docker network.

## End-to-end seed run

```bash
# 1. Auth via browser (creates a `users` row + stores OAuth tokens)
open https://shillscore.tg-itsavibe.com/auth/twitter

# 2. Trigger the seed: pulls follow list, syncs each account, parses mentions,
#    fetches per-mention price anchors, writes them back.
make shell-api
python -m shillscore seed --user theogonella

# 3. Wait for the queue to drain (watch worker logs)
make logs

# 4. Inspect the result — visit https://shillscore.tg-itsavibe.com/ or query
#    the materialized views directly:
make psql
shillscore=> REFRESH MATERIALIZED VIEW mention_returns;
shillscore=> REFRESH MATERIALIZED VIEW account_leaderboard_cohort;
shillscore=> SELECT cohort, handle, n_closed, median_excess
             FROM account_leaderboard_cohort
             WHERE cohort='30d'
             ORDER BY median_excess * sqrt(n_closed::float / (n_closed + 5))
                      DESC NULLS LAST LIMIT 25;
```

A daily cron (`06:00–06:30 UTC`) keeps benchmarks, daily price series, and both materialized views fresh, plus runs the bootstrap-CI pass.

## UI surface (Phase 2)

| Route | What it shows |
|---|---|
| `/` | Top-3 podium (Follow-on-X CTAs), equity-curve chart, token-charts grid ("who caught the winners"), full leaderboard table. Cohort + sort + **view** drive everything. The view is a three-way concentration split (Path A): **Scouts** (default) — handles whose single most-mentioned token is under 50% of their matured calls, i.e. diversified callers; **Insiders** — handles ≥50% concentrated on one token, overwhelmingly project accounts ranked on their own coin; **All** — unfiltered. Scores are always the honest full-record aggregate — the view only partitions handles, it does not re-score them. `?view=insiders` / `?view=all` switch tabs. |
| `/account/[handle]` | Per-account stats per cohort — honest full-record aggregate, with a Scout/Insider badge and per-cohort concentration (distinct-token count + top-token share). Mention-curves chart, full mention list. |
| `/mention/[id]` | Tweet + price chart + open/closed status across cohorts. |

API endpoints powering the UI: `/api/leaderboard`, `/api/leaderboard/equity-curves`, `/api/leaderboard/token-charts`, `/api/account/{handle}`, `/api/account/{handle}/mention-curves`, `/api/mention/{id}`, `/api/mention/{id}/series`.

## License

MIT — see `LICENSE`.

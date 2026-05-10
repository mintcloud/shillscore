# shillscore

Accuracy tracker for crypto Twitter calls. Watch the people you follow, see how their picks actually played out, follow the ones with real edge.

> **Status: Phase 1 — single-user MVP backend.** OAuth 2.0 PKCE, Twitter sync, mention parsing, CoinGecko price anchors, daily series + benchmark cron, materialized views for `mention_returns` and `account_leaderboard`, bootstrap CI nightly. UI is still Phase 0; leaderboard SQL is the acceptance surface. See [`docs/plan.md`](docs/plan.md).

## What it does

When a Twitter account you follow mentions a token, shillscore captures the price at that moment, then tracks it forward. Over time each account accumulates a track record: hit rate, median excess return vs BTC, time-to-peak, drawdown. You see the leaderboard filtered to *just the accounts you follow*, so it's your network's signal — not a global pump-noise feed.

## Why

Crypto Twitter is full of confident calls. Almost no-one keeps receipts. shillscore keeps receipts.

## Stack

| Layer | Choice |
|---|---|
| Frontend | Next.js 15 (App Router) + Tailwind, dark theme, self-hosted (standalone build) |
| API | FastAPI 3.12 + SQLAlchemy 2.0 + Alembic |
| Worker | arq on Redis 7 |
| DB | Postgres 16 |
| Price source | CoinGecko (DefiLlama fallback later) |
| Auth | Twitter OAuth 2.0 PKCE primary; GitHub OAuth secondary |
| Hosting | Hetzner VPS, Cloudflare tunnel, all in one `infra/docker-compose.yml` |

## Repo layout

```
apps/web/                  Next.js frontend
apps/api/                  FastAPI app + arq worker (same image)
apps/api/migrations/       Alembic migrations
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
make migrate          # 0001 schema + 0002 views + user oauth columns
curl http://localhost:3006/                                    # web
docker compose -f infra/docker-compose.yml exec api \
  curl -s http://localhost:8000/health
```

The web container binds to `127.0.0.1:3006` so the Cloudflare tunnel can route `shillscore.tg-itsavibe.com` to it. See [`infra/cloudflare-tunnel.md`](infra/cloudflare-tunnel.md) for the tunnel ingress entry. Both `/api/*` and `/auth/*` are proxied from web → api over the internal docker network.

## Phase 1: end-to-end seed run

```bash
# 1. Auth via browser (creates a `users` row + stores OAuth tokens)
open https://shillscore.tg-itsavibe.com/auth/twitter

# 2. Trigger the seed: pulls follow list, syncs each account, parses mentions,
#    fetches per-mention price anchors, writes them back.
make shell-api
python -m shillscore seed --user theogonella

# 3. Wait for the queue to drain (watch worker logs)
make logs

# 4. Inspect the leaderboard
make psql
shillscore=> REFRESH MATERIALIZED VIEW mention_returns;
shillscore=> REFRESH MATERIALIZED VIEW account_leaderboard;
shillscore=> SELECT handle, n_closed, median_excess
             FROM account_leaderboard
             ORDER BY median_excess DESC NULLS LAST LIMIT 25;
```

A daily cron (`06:00–06:30 UTC`) keeps benchmarks, daily price series, and both materialized views fresh, plus runs the bootstrap-CI pass.

## License

MIT — see `LICENSE`.

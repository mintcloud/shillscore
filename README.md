# shillscore

Accuracy tracker for crypto Twitter calls. Watch the people you follow, see how their picks actually played out, follow the ones with real edge.

> **Status: Phase 0 scaffold.** Compose stack + FastAPI/arq/Next.js skeletons + initial migration are in. No business logic yet — Phase 1 brings the worker pipeline. See [`docs/plan.md`](docs/plan.md) for the canonical build plan.

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

## Running locally

```bash
cp infra/.env.example infra/.env
# fill in POSTGRES_PASSWORD, SESSION_SECRET, TWITTER_*, GITHUB_* (Phase 1+)

make build
make up
make migrate          # apply initial schema
curl http://localhost:3006/   # web
docker compose -f infra/docker-compose.yml exec api curl -s http://localhost:8000/health
```

The web container binds to `127.0.0.1:3006` so the Cloudflare tunnel can route `shillscore.tg-itsavibe.com` to it. See [`infra/cloudflare-tunnel.md`](infra/cloudflare-tunnel.md) for the tunnel ingress entry.

## License

MIT — see `LICENSE`.

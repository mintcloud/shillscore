# shillscore

Accuracy tracker for crypto Twitter calls. Watch the people you follow, see how their picks actually played out, follow the ones with real edge.

> **Status: WIP.** Scaffold only — no app code yet. See `docs/plan.md` for the build plan.

## What it does

When a Twitter account you follow mentions a token, shillscore captures the price at that moment, then tracks it forward. Over time each account accumulates a track record: hit rate, average return, time-to-peak, drawdown. You see the leaderboard filtered to *just the accounts you follow*, so it's your network's signal — not a global pump-noise feed.

## Why

Crypto Twitter is full of confident calls. Almost no-one keeps receipts. shillscore keeps receipts.

## Stack (planned)

- **Frontend** — Next.js + Tailwind, dark theme
- **Backend** — FastAPI (Python) or Hono (TS), TBD in `docs/decisions/`
- **DB** — Postgres
- **Ingest** — Twitter API v2 polling job + hourly price snapshot job (CoinGecko / DefiLlama)
- **Hosting** — TBD (Fly / Railway / Hetzner)

## Repo layout

```
apps/web/          Next.js frontend
apps/api/          API server
packages/shared/   Types shared between web + api
infra/             docker-compose, SQL migrations
scripts/           Ingest + price-snapshot jobs
docs/              Plan, data model, decision log
```

## Running locally

Not yet runnable. Will be `docker compose up` once `infra/docker-compose.yml` lands.

## License

MIT — see `LICENSE`.

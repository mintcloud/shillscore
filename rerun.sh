#!/usr/bin/env bash
# Full ticker-resolution rerun for shillscore.
#
# Run from ~/projects/shillscore on the VPS. Reads ./infra/.env so make sure
# COINGECKO_API_KEY is set there before running. Idempotent on every step
# *except* the wipe (step 5) — if you stop and resume, restart from step 5.
#
# Decisions baked in:
#   - tickers-only wipe (preserves contract-resolved tokens + their mentions)
#   - batched per-token anchor sweep (anchor_all_pending) instead of
#     per-mention on_new_mention spam
#   - free CG demo API key (CG-MZx1RwwScm3GTGxQoZYRxgy6)
#
# After this finishes, leaderboard counts will be substantially higher and
# more accurate. Spot-check with:
#   SELECT t.symbol, count(*) FROM mentions m JOIN tokens t ON t.id=m.token_id
#   WHERE match_kind='ticker' GROUP BY 1 HAVING count(*)>=3 ORDER BY 2 DESC LIMIT 30;

set -euo pipefail

COMPOSE="docker compose -f infra/docker-compose.yml --env-file infra/.env"

CG_KEY="${CG_KEY:-CG-MZx1RwwScm3GTGxQoZYRxgy6}"

say()  { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
psql() { $COMPOSE exec -T postgres psql -U shillscore -d shillscore "$@"; }

# ---------- 1. Wire the CG API key into infra/.env -----------------------
say "1/9 — wiring COINGECKO_API_KEY into infra/.env"
if grep -q '^COINGECKO_API_KEY=' infra/.env; then
  # Replace existing line.
  sed -i.bak "s|^COINGECKO_API_KEY=.*|COINGECKO_API_KEY=${CG_KEY}|" infra/.env
else
  echo "COINGECKO_API_KEY=${CG_KEY}" >> infra/.env
fi
echo "infra/.env now has:"; grep '^COINGECKO_API_KEY=' infra/.env

# ---------- 2. Rebuild the api/worker images ----------------------------
# Dockerfile gained `COPY scripts ./scripts`; migrations dir gained 0005.
# Both ride along with --no-cache to make sure pip layer doesn't mask
# updated source.
say "2/9 — rebuilding api + worker images (~3 min)"
$COMPOSE build --no-cache api worker
$COMPOSE up -d

# ---------- 3. Apply migration 0005 -------------------------------------
say "3/9 — applying alembic migration 0005 (aliases + ambiguous_candidates)"
$COMPOSE exec api alembic upgrade head

# ---------- 4. Stop worker so the wipe isn't racing live anchor jobs ----
say "4/9 — pausing worker for the wipe"
$COMPOSE stop worker

# ---------- 5. Tickers-only wipe ----------------------------------------
say "5/9 — running wipe_tickers.sql"
psql < sql/wipe_tickers.sql

echo "Post-wipe sanity:"
psql -c "SELECT
  (SELECT count(*) FROM tokens WHERE contract_addr IS NOT NULL) AS contract_tokens_kept,
  (SELECT count(*) FROM tokens WHERE contract_addr IS NULL)     AS ticker_tokens_remaining,
  (SELECT count(*) FROM mentions WHERE token_id IS NOT NULL)    AS mentions_with_token,
  (SELECT count(*) FROM raw_tweets WHERE resolved_at IS NULL)   AS raw_tweets_pending;"

# ---------- 6. Recreate matviews ----------------------------------------
say "6/9 — recreating mention_returns + account_leaderboard_cohort"
psql < sql/recreate_matviews.sql

# ---------- 7. Clear redis search cache ---------------------------------
# Old $-prefixed search misses cached for 1h would re-poison resolution.
# `cg:contract:*` keys stay (they were always correct).
say "7/9 — clearing redis cg:search:* cache"
$COMPOSE exec -T redis sh -c 'redis-cli --scan --pattern "cg:search:*" | xargs -r redis-cli del'

# ---------- 8. Restart worker with per-mention anchor suppression --------
# SHILLSCORE_SKIP_PER_MENTION_ANCHOR=1 makes resolve_pending_tweets stop
# enqueueing on_new_mention. We do anchors in one bulk anchor_all_pending
# call after the resolve sweep drains — N CG calls → M (~3-10× win).
say "8/9 — restarting worker with bulk-rerun env flag"
$COMPOSE stop worker || true
$COMPOSE rm -f worker || true
# `compose run` does not inherit shell env by default — pass via -e so the
# new arq worker actually sees the flag and skips per-mention enqueues.
$COMPOSE run -d --name shillscore-worker-rerun \
  -e SHILLSCORE_SKIP_PER_MENTION_ANCHOR=1 \
  worker python -m arq app.worker.main.WorkerSettings

# Prime tokens (16s with key, ~32s without).
say "8a — priming tokens from CG top-1000 markets"
$COMPOSE exec api python -m scripts.prime_tokens

# Kick the resolve sweep.
say "8b — kicking resolve_pending_sweep"
$COMPOSE exec api python -c "
import asyncio
from arq.connections import RedisSettings, create_pool
async def go():
    r = await create_pool(RedisSettings.from_dsn('redis://redis:6379/0'))
    j = await r.enqueue_job('resolve_pending_sweep')
    print('enqueued', j.job_id)
asyncio.run(go())
"

# Poll until raw_tweets backlog drains (sweeper only batches 1000 at a time
# so for big sets you may need to enqueue resolve_pending_sweep again).
say "8c — waiting for resolve to drain (polling every 30s)"
while true; do
  pending=$(psql -tA -c "SELECT count(*) FROM raw_tweets WHERE resolved_at IS NULL AND resolve_attempts < 5;")
  done_count=$(psql -tA -c "SELECT count(*) FROM raw_tweets WHERE resolved_at IS NOT NULL;")
  printf "  pending=%s  done=%s\n" "$pending" "$done_count"
  if [ "$pending" -le 0 ]; then
    break
  fi
  # Re-kick the sweep every loop in case the in-memory queue went idle.
  $COMPOSE exec -T api python -c "
import asyncio
from arq.connections import RedisSettings, create_pool
async def go():
    r = await create_pool(RedisSettings.from_dsn('redis://redis:6379/0'))
    await r.enqueue_job('resolve_pending_sweep')
asyncio.run(go())
" >/dev/null 2>&1 || true
  sleep 30
done
echo "  resolve drained."

# ---------- 9. Batched anchor sweep + matview refresh -------------------
say "9/9 — anchor_all_pending (one CG call per token) + matview refresh"
$COMPOSE exec api python -c "
import asyncio
from arq.connections import RedisSettings, create_pool
async def go():
    r = await create_pool(RedisSettings.from_dsn('redis://redis:6379/0'))
    j = await r.enqueue_job('anchor_all_pending')
    print('enqueued anchor_all_pending', j.job_id)
asyncio.run(go())
"

# Wait until un-anchored mentions drop to a steady state (3 zero-delta samples).
say "9a — waiting for anchor_all_pending to drain"
prev=-1; zero_passes=0
while true; do
  unanchored=$(psql -tA -c "
    SELECT count(*) FROM mentions
    WHERE price_at_mention IS NULL
      AND token_id IS NOT NULL;")
  printf "  unanchored=%s\n" "$unanchored"
  if [ "$unanchored" = "$prev" ]; then
    zero_passes=$((zero_passes + 1))
  else
    zero_passes=0
  fi
  prev=$unanchored
  if [ "$zero_passes" -ge 3 ]; then
    break
  fi
  sleep 20
done

# Re-create CONCURRENTLY-safe state by refreshing matviews + CI.
say "9b — refresh_mention_returns + bootstrap_account_ci"
$COMPOSE exec -T api python -c "
import asyncio
from arq.connections import RedisSettings, create_pool
async def go():
    r = await create_pool(RedisSettings.from_dsn('redis://redis:6379/0'))
    await r.enqueue_job('refresh_mention_returns')
    await r.enqueue_job('bootstrap_account_ci')
    print('enqueued refresh + ci')
asyncio.run(go())
"
sleep 30  # give the cron-style jobs time to run; cheap to overestimate.

# ---------- Wrap up ------------------------------------------------------
say "DONE — restoring normal worker (no SKIP env)"
docker stop shillscore-worker-rerun 2>/dev/null || true
docker rm   shillscore-worker-rerun 2>/dev/null || true
$COMPOSE up -d worker

say "Final stats"
psql -c "
SELECT
  (SELECT count(*) FROM raw_tweets)                                    AS raw_tweets_total,
  (SELECT count(*) FROM raw_tweets WHERE resolved_at IS NULL)          AS raw_tweets_pending,
  (SELECT count(*) FROM tokens)                                        AS tokens_total,
  (SELECT count(*) FROM tokens WHERE contract_addr IS NOT NULL)        AS tokens_contract,
  (SELECT count(*) FROM tokens WHERE contract_addr IS NULL)            AS tokens_ticker,
  (SELECT count(*) FROM mentions)                                      AS mentions_total,
  (SELECT count(*) FROM mentions WHERE price_at_mention IS NOT NULL)   AS mentions_anchored,
  (SELECT count(*) FROM mentions WHERE token_id IS NULL
                              AND ambiguous_candidates IS NOT NULL)    AS mentions_ambiguous,
  (SELECT count(*) FROM account_token_aliases)                         AS aliases_learned,
  (SELECT count(*) FROM account_leaderboard_cohort)                    AS leaderboard_rows;
"
echo
echo "Spot-check the top resolved tickers:"
psql -c "
SELECT t.symbol, t.coingecko_id, t.name, count(*) AS n
FROM mentions m JOIN tokens t ON t.id = m.token_id
WHERE m.match_kind = 'ticker'
GROUP BY t.symbol, t.coingecko_id, t.name
ORDER BY n DESC
LIMIT 30;
"
echo
echo "Top ambiguous symbols (candidates for manual alias seeding later):"
psql -c "
SELECT upper(regexp_replace(raw_match, '^\\\$', '')) AS sym, count(*) AS n
FROM mentions
WHERE token_id IS NULL AND ambiguous_candidates IS NOT NULL
GROUP BY 1 ORDER BY n DESC LIMIT 20;
"

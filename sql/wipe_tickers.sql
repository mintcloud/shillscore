-- Tickers-only wipe: keeps contract-resolved tokens + their mentions/prices.
-- The pre-fix /search-with-$ bug only affected ticker resolution; contract
-- hits via /coins/{chain}/contract/{addr} were always correct.
--
-- Run after migration 0005 is applied (account_token_aliases + mentions
-- .ambiguous_candidates exist). The DROP MATERIALIZED VIEW lines are
-- defensive — recreate_matviews.sql will rebuild them next.

BEGIN;

-- Matviews depend on mentions+token_prices; drop them so the cascades below
-- don't run into "cannot drop tokens because matview depends on it".
DROP VIEW              IF EXISTS account_leaderboard;
DROP MATERIALIZED VIEW IF EXISTS account_leaderboard_cohort;
DROP MATERIALIZED VIEW IF EXISTS mention_returns;

-- Delete ticker-only token graph. CASCADE flows from tokens → token_prices
-- and tokens → mentions (FK on mentions.token_id) for the ticker rows.
-- Contract-resolved tokens (contract_addr IS NOT NULL) are correct and stay.
DELETE FROM token_prices
 WHERE token_id IN (SELECT id FROM tokens WHERE contract_addr IS NULL);

DELETE FROM mentions
 WHERE token_id IN (SELECT id FROM tokens WHERE contract_addr IS NULL);

-- Also wipe any orphan ambiguous mentions left from a previous run (token_id
-- IS NULL but ambiguous_candidates set). Cheap; should be zero on a first
-- re-run since migration 0005 just added the column.
DELETE FROM mentions
 WHERE token_id IS NULL AND ambiguous_candidates IS NOT NULL;

DELETE FROM tokens WHERE contract_addr IS NULL;

-- Account-scoped aliases — table is brand-new at this point, but TRUNCATE
-- is defensive against re-running this script.
TRUNCATE account_token_aliases;

-- Mark every raw_tweet as needing re-resolution. Idempotent on the
-- contract side: ON CONFLICT DO NOTHING on the (tweet_id, token_id)
-- unique constraint short-circuits duplicate contract mentions, while
-- the fixed parser picks up any tickers the old code silently dropped.
UPDATE raw_tweets
   SET resolved_at = NULL,
       resolve_attempts = 0,
       resolve_last_error = NULL;

COMMIT;

-- Sanity check after wipe — paste this into psql to verify state:
--
--   SELECT
--     (SELECT count(*) FROM tokens WHERE contract_addr IS NOT NULL) AS contract_tokens_kept,
--     (SELECT count(*) FROM tokens WHERE contract_addr IS NULL)     AS ticker_tokens_remaining,
--     (SELECT count(*) FROM mentions WHERE token_id IS NOT NULL)    AS mentions_with_token,
--     (SELECT count(*) FROM raw_tweets WHERE resolved_at IS NULL)   AS raw_tweets_pending;
--
-- Expectation:
--   contract_tokens_kept   >= 1 (whatever you had pre-wipe)
--   ticker_tokens_remaining = 0
--   mentions_with_token    = (count of pre-wipe contract mentions)
--   raw_tweets_pending     = total raw_tweets count

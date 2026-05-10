"""user OAuth tokens + materialized views + account_ci

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-05
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # OAuth 2.0 token columns on users (per-user, refreshed on use).
    op.execute("""
    ALTER TABLE users
      ADD COLUMN twitter_access_token   TEXT,
      ADD COLUMN twitter_refresh_token  TEXT,
      ADD COLUMN twitter_token_expires_at TIMESTAMPTZ;
    """)

    # Mention-level returns (the t0/p0 anchor + horizon prices off the daily series).
    op.execute("""
    CREATE MATERIALIZED VIEW mention_returns AS
    WITH price_at AS (
      SELECT m.id,
             m.account_id,
             m.token_id,
             m.tweet_ts,
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
           CASE WHEN p0 > 0 AND p_1d   IS NOT NULL THEN (p_1d   - p0) / p0 END AS r_1d,
           CASE WHEN p0 > 0 AND p_7d   IS NOT NULL THEN (p_7d   - p0) / p0 END AS r_7d,
           CASE WHEN p0 > 0 AND p_30d  IS NOT NULL THEN (p_30d  - p0) / p0 END AS r_30d,
           CASE WHEN p0 > 0 AND p_90d  IS NOT NULL THEN (p_90d  - p0) / p0 END AS r_90d,
           CASE WHEN p0 > 0 AND p_180d IS NOT NULL THEN (p_180d - p0) / p0 END AS r_180d,
           CASE WHEN p0 > 0 AND p_365d IS NOT NULL THEN (p_365d - p0) / p0 END AS r_365d,
           CASE
             WHEN p0 > 0 AND p_365d IS NOT NULL AND p_btc_365d IS NOT NULL AND p_btc_t0 > 0
             THEN (p_365d - p0) / p0 - (p_btc_365d - p_btc_t0) / p_btc_t0
           END AS r_365d_excess,
           (tweet_ts + INTERVAL '365 days' < now()) AS is_closed
    FROM price_at;
    """)
    op.execute("CREATE UNIQUE INDEX mention_returns_id_uq ON mention_returns (id);")
    op.execute("CREATE INDEX mention_returns_account_idx ON mention_returns (account_id);")

    # Account-level leaderboard (closed mentions only, min N = 10).
    op.execute("""
    CREATE MATERIALIZED VIEW account_leaderboard AS
    SELECT a.id, a.handle, a.display_name, a.followers_count,
           count(*)                                         AS n_closed,
           count(*) FILTER (WHERE r_365d_excess > 0)        AS n_winners,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY r_365d_excess) AS median_excess,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY r_365d)        AS median_raw,
           avg(r_365d_excess)                               AS mean_excess
    FROM mention_returns mr
    JOIN accounts a ON a.id = mr.account_id
    WHERE is_closed AND r_365d_excess IS NOT NULL
    GROUP BY a.id
    HAVING count(*) >= 10;
    """)
    op.execute("CREATE UNIQUE INDEX account_leaderboard_id_uq ON account_leaderboard (id);")

    # Account-level CI (populated nightly by bootstrap_account_ci).
    op.execute("""
    CREATE TABLE account_ci (
      account_id   BIGINT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
      median_excess NUMERIC(20,8),
      ci_low_excess NUMERIC(20,8),
      ci_high_excess NUMERIC(20,8),
      n_closed     INT,
      computed_at  TIMESTAMPTZ DEFAULT now()
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS account_ci;")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS account_leaderboard;")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mention_returns;")
    op.execute("""
    ALTER TABLE users
      DROP COLUMN IF EXISTS twitter_token_expires_at,
      DROP COLUMN IF EXISTS twitter_refresh_token,
      DROP COLUMN IF EXISTS twitter_access_token;
    """)

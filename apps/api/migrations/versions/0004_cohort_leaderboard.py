"""cohort-parameterized leaderboard — 30d / 90d / 365d, BTC-excess at each horizon

Path B from phase 2 spike: extend mention_returns with r_30d_excess + r_90d_excess
(needs BTC anchor + 30d / 90d horizon prices), then replace the single-cohort
account_leaderboard with a long-format account_leaderboard_cohort view that has
one row per (account_id, cohort). Min N is 1 — sqrt(N) damping at API time
handles low-confidence ranks.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # mention_returns: add 30d / 90d BTC anchors and excess columns, plus per-cohort
    # is_closed flags. Drop and recreate (CONCURRENTLY can't change column set).
    op.execute("DROP MATERIALIZED VIEW IF EXISTS account_leaderboard;")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mention_returns;")

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
              WHERE symbol='BTC' AND ts <= m.tweet_ts
              ORDER BY ts DESC LIMIT 1) AS p_btc_t0,
             (SELECT close_usd FROM benchmark_prices
              WHERE symbol='BTC'
                AND ts <= m.tweet_ts + INTERVAL '30 days'
              ORDER BY ts DESC LIMIT 1) AS p_btc_30d,
             (SELECT close_usd FROM benchmark_prices
              WHERE symbol='BTC'
                AND ts <= m.tweet_ts + INTERVAL '90 days'
              ORDER BY ts DESC LIMIT 1) AS p_btc_90d,
             (SELECT close_usd FROM benchmark_prices
              WHERE symbol='BTC'
                AND ts <= m.tweet_ts + INTERVAL '365 days'
              ORDER BY ts DESC LIMIT 1) AS p_btc_365d
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
             WHEN p0 > 0 AND p_30d IS NOT NULL AND p_btc_30d IS NOT NULL AND p_btc_t0 > 0
             THEN (p_30d - p0) / p0 - (p_btc_30d - p_btc_t0) / p_btc_t0
           END AS r_30d_excess,
           CASE
             WHEN p0 > 0 AND p_90d IS NOT NULL AND p_btc_90d IS NOT NULL AND p_btc_t0 > 0
             THEN (p_90d - p0) / p0 - (p_btc_90d - p_btc_t0) / p_btc_t0
           END AS r_90d_excess,
           CASE
             WHEN p0 > 0 AND p_365d IS NOT NULL AND p_btc_365d IS NOT NULL AND p_btc_t0 > 0
             THEN (p_365d - p0) / p0 - (p_btc_365d - p_btc_t0) / p_btc_t0
           END AS r_365d_excess,
           (tweet_ts + INTERVAL '30 days'  < now()) AS is_closed_30d,
           (tweet_ts + INTERVAL '90 days'  < now()) AS is_closed_90d,
           (tweet_ts + INTERVAL '365 days' < now()) AS is_closed_365d,
           (tweet_ts + INTERVAL '365 days' < now()) AS is_closed
    FROM price_at;
    """)
    op.execute("CREATE UNIQUE INDEX mention_returns_id_uq ON mention_returns (id);")
    op.execute("CREATE INDEX mention_returns_account_idx ON mention_returns (account_id);")

    # Long-format leaderboard view: one row per (account_id, cohort).
    # min N = 1 — sqrt(N) damping at API time disciplines the rank.
    op.execute("""
    CREATE MATERIALIZED VIEW account_leaderboard_cohort AS
    WITH per_cohort AS (
      SELECT a.id AS account_id, a.handle, a.display_name, a.followers_count,
             '30d'::text AS cohort,
             count(*)                                              AS n_closed,
             count(*) FILTER (WHERE r_30d_excess > 0)              AS n_winners,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY r_30d_excess) AS median_excess,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY r_30d)        AS median_raw,
             avg(r_30d_excess)                                     AS mean_excess
      FROM mention_returns mr
      JOIN accounts a ON a.id = mr.account_id
      WHERE is_closed_30d AND r_30d_excess IS NOT NULL
      GROUP BY a.id

      UNION ALL

      SELECT a.id, a.handle, a.display_name, a.followers_count,
             '90d',
             count(*),
             count(*) FILTER (WHERE r_90d_excess > 0),
             percentile_cont(0.5) WITHIN GROUP (ORDER BY r_90d_excess),
             percentile_cont(0.5) WITHIN GROUP (ORDER BY r_90d),
             avg(r_90d_excess)
      FROM mention_returns mr
      JOIN accounts a ON a.id = mr.account_id
      WHERE is_closed_90d AND r_90d_excess IS NOT NULL
      GROUP BY a.id

      UNION ALL

      SELECT a.id, a.handle, a.display_name, a.followers_count,
             '365d',
             count(*),
             count(*) FILTER (WHERE r_365d_excess > 0),
             percentile_cont(0.5) WITHIN GROUP (ORDER BY r_365d_excess),
             percentile_cont(0.5) WITHIN GROUP (ORDER BY r_365d),
             avg(r_365d_excess)
      FROM mention_returns mr
      JOIN accounts a ON a.id = mr.account_id
      WHERE is_closed_365d AND r_365d_excess IS NOT NULL
      GROUP BY a.id
    )
    SELECT * FROM per_cohort;
    """)
    op.execute(
        "CREATE UNIQUE INDEX account_leaderboard_cohort_uq "
        "ON account_leaderboard_cohort (account_id, cohort);"
    )
    op.execute(
        "CREATE INDEX account_leaderboard_cohort_idx "
        "ON account_leaderboard_cohort (cohort);"
    )

    # Backwards-compat alias: keep `account_leaderboard` as a regular view that
    # selects the 365d cohort. Refresh worker job and CLI hints still target the
    # old name; this avoids touching them in this migration.
    op.execute("""
    CREATE VIEW account_leaderboard AS
    SELECT account_id AS id, handle, display_name, followers_count,
           n_closed, n_winners, median_excess, median_raw, mean_excess
    FROM account_leaderboard_cohort
    WHERE cohort = '365d';
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS account_leaderboard;")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS account_leaderboard_cohort;")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mention_returns;")

    # Restore prior shape (copied from 0002 upgrade).
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

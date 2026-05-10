"""raw_tweets — durable boundary between X fetch and CG resolve

Splits sync_batch into Phase A (Twitter fetch + persist raw) and Phase B
(CG resolve + write mentions). A CoinGecko outage leaves rows in raw_tweets
with resolved_at NULL; a sweeper retries them without re-billing X.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-07
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE raw_tweets (
      id                 BIGSERIAL PRIMARY KEY,
      tweet_id           TEXT UNIQUE NOT NULL,
      account_id         BIGINT NOT NULL REFERENCES accounts(id),
      tweet_ts           TIMESTAMPTZ NOT NULL,
      tweet_text         TEXT NOT NULL,
      raw_json           JSONB NOT NULL,
      fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
      resolved_at        TIMESTAMPTZ,
      resolve_attempts   SMALLINT NOT NULL DEFAULT 0,
      resolve_last_error TEXT
    );
    """)

    # Sweeper hits this index every cron tick.
    op.execute("""
    CREATE INDEX raw_tweets_unresolved_idx
      ON raw_tweets (fetched_at)
      WHERE resolved_at IS NULL;
    """)

    # Useful for per-account introspection in admin.
    op.execute("CREATE INDEX raw_tweets_account_idx ON raw_tweets (account_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS raw_tweets_account_idx;")
    op.execute("DROP INDEX IF EXISTS raw_tweets_unresolved_idx;")
    op.execute("DROP TABLE IF EXISTS raw_tweets;")

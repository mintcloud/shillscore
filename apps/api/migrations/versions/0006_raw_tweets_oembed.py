"""raw_tweets.oembed_html — cached X embed markup for hover rendering

Stores the HTML blockquote returned by publish.twitter.com/oembed so the
frontend can hand it straight to widgets.js for a real branded card.
oEmbed responses set cache_age=3153600000s (~100y) so once fetched we
never refetch unless the tweet is deleted.

`oembed_fetched_at` is the success timestamp. `oembed_error` records the
last failure reason — non-NULL means "we tried and X returned something
we couldn't use" (deleted, suspended, private). The frontend should fall
back to plain text in that case.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-13
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE raw_tweets
      ADD COLUMN oembed_html        TEXT,
      ADD COLUMN oembed_fetched_at  TIMESTAMPTZ,
      ADD COLUMN oembed_error       TEXT;
    """)
    # Sweeper looks for rows that need an oEmbed fetch: never tried + not
    # known-broken. Partial index keeps it tiny.
    op.execute("""
    CREATE INDEX raw_tweets_oembed_pending_idx
      ON raw_tweets (tweet_ts DESC)
      WHERE oembed_html IS NULL AND oembed_error IS NULL;
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS raw_tweets_oembed_pending_idx;")
    op.execute("""
    ALTER TABLE raw_tweets
      DROP COLUMN IF EXISTS oembed_error,
      DROP COLUMN IF EXISTS oembed_fetched_at,
      DROP COLUMN IF EXISTS oembed_html;
    """)

"""account_token_aliases + mentions.ambiguous_candidates

Three-tier ticker resolution support:
- `account_token_aliases` stores per-account (symbol → token_id) priors,
  populated when an account drops a contract address alongside a cashtag
  for the same logical token. Resolver consults this BEFORE picking a
  candidate by mcap rank, so memecoin shillers route to their actual
  token instead of whatever has the biggest mcap among the lookalikes.
- `mentions.ambiguous_candidates` lets us record a mention whose ticker
  has no clear winner (multiple symbol-matched CG entries, none with a
  decisive mcap lead). token_id stays NULL; leaderboard views must
  filter token_id IS NOT NULL (already the case via JOIN).

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-11
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE account_token_aliases (
      account_id          BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
      symbol              TEXT NOT NULL,
      token_id            BIGINT NOT NULL REFERENCES tokens(id) ON DELETE CASCADE,
      last_seen_tweet_id  TEXT,
      updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (account_id, symbol)
    );
    CREATE INDEX account_token_aliases_token_idx
      ON account_token_aliases (token_id);
    """)

    op.execute("""
    ALTER TABLE mentions
      ADD COLUMN ambiguous_candidates JSONB;
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE mentions DROP COLUMN IF EXISTS ambiguous_candidates;")
    op.execute("DROP TABLE IF EXISTS account_token_aliases;")

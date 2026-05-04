"""initial schema — accounts, tokens, mentions, token_prices, benchmark_prices, users, user_follows

Revision ID: 0001
Revises:
Create Date: 2026-05-04
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE accounts (
      id              BIGSERIAL PRIMARY KEY,
      twitter_id      TEXT UNIQUE NOT NULL,
      handle          TEXT UNIQUE NOT NULL,
      display_name    TEXT,
      followers_count INT,
      last_synced_at  TIMESTAMPTZ,
      last_tweet_id   TEXT,
      oldest_tweet_id TEXT,
      lookback_days   INT DEFAULT 90,
      first_seen_at   TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX accounts_last_synced_at_idx ON accounts (last_synced_at);
    """)

    op.execute("""
    CREATE TABLE tokens (
      id            BIGSERIAL PRIMARY KEY,
      coingecko_id  TEXT UNIQUE,
      symbol        TEXT NOT NULL,
      name          TEXT,
      contract_addr TEXT,
      chain         TEXT,
      is_verified   BOOLEAN DEFAULT FALSE
    );
    CREATE UNIQUE INDEX tokens_chain_contract_uq
      ON tokens (chain, contract_addr) WHERE contract_addr IS NOT NULL;
    """)

    op.execute("""
    CREATE TABLE mentions (
      id                  BIGSERIAL PRIMARY KEY,
      account_id          BIGINT REFERENCES accounts(id),
      tweet_id            TEXT NOT NULL,
      tweet_ts            TIMESTAMPTZ NOT NULL,
      tweet_text          TEXT NOT NULL,
      token_id            BIGINT REFERENCES tokens(id),
      raw_match           TEXT,
      match_kind          TEXT,
      sentiment           TEXT,
      is_self_quote       BOOLEAN,
      price_at_mention    NUMERIC(30,10),
      price_at_mention_ts TIMESTAMPTZ,
      price_anchor_kind   TEXT,
      price_source        TEXT,
      CONSTRAINT u_mention UNIQUE (tweet_id, token_id)
    );
    CREATE INDEX mentions_account_ts_idx ON mentions (account_id, tweet_ts DESC);
    CREATE INDEX mentions_token_ts_idx   ON mentions (token_id, tweet_ts);
    """)

    op.execute("""
    CREATE TABLE token_prices (
      token_id    BIGINT REFERENCES tokens(id),
      ts          TIMESTAMPTZ NOT NULL,
      close_usd   NUMERIC(30,10) NOT NULL,
      granularity TEXT NOT NULL,
      source      TEXT,
      PRIMARY KEY (token_id, ts, granularity)
    );
    CREATE INDEX token_prices_token_ts_idx     ON token_prices (token_id, ts DESC);
    CREATE INDEX token_prices_token_gran_ts_idx ON token_prices (token_id, granularity, ts);
    """)

    op.execute("""
    CREATE TABLE benchmark_prices (
      symbol    TEXT NOT NULL,
      ts        TIMESTAMPTZ NOT NULL,
      close_usd NUMERIC(30,10) NOT NULL,
      PRIMARY KEY (symbol, ts)
    );
    """)

    op.execute("""
    CREATE TABLE users (
      id           BIGSERIAL PRIMARY KEY,
      twitter_id   TEXT UNIQUE NOT NULL,
      handle       TEXT NOT NULL,
      github_login TEXT,
      joined_at    TIMESTAMPTZ DEFAULT now(),
      last_sync_at TIMESTAMPTZ
    );
    """)

    op.execute("""
    CREATE TABLE user_follows (
      user_id    BIGINT REFERENCES users(id) ON DELETE CASCADE,
      account_id BIGINT REFERENCES accounts(id),
      PRIMARY KEY (user_id, account_id)
    );
    CREATE INDEX user_follows_account_idx ON user_follows (account_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_follows;")
    op.execute("DROP TABLE IF EXISTS users;")
    op.execute("DROP TABLE IF EXISTS benchmark_prices;")
    op.execute("DROP TABLE IF EXISTS token_prices;")
    op.execute("DROP TABLE IF EXISTS mentions;")
    op.execute("DROP TABLE IF EXISTS tokens;")
    op.execute("DROP TABLE IF EXISTS accounts;")

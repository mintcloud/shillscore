from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    twitter_id: Mapped[str] = mapped_column(Text, unique=True)
    handle: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    followers_count: Mapped[int | None] = mapped_column(Integer)
    last_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_tweet_id: Mapped[str | None] = mapped_column(Text)
    oldest_tweet_id: Mapped[str | None] = mapped_column(Text)
    lookback_days: Mapped[int] = mapped_column(Integer, default=90)
    first_seen_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class Token(Base):
    __tablename__ = "tokens"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    coingecko_id: Mapped[str | None] = mapped_column(Text, unique=True)
    symbol: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    contract_addr: Mapped[str | None] = mapped_column(Text)
    chain: Mapped[str | None] = mapped_column(Text)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)


class Mention(Base):
    __tablename__ = "mentions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    tweet_id: Mapped[str] = mapped_column(Text)
    tweet_ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    tweet_text: Mapped[str] = mapped_column(Text)
    token_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tokens.id"))
    raw_match: Mapped[str | None] = mapped_column(Text)
    match_kind: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[str | None] = mapped_column(Text)
    is_self_quote: Mapped[bool | None] = mapped_column(Boolean)
    price_at_mention: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    price_at_mention_ts: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    price_anchor_kind: Mapped[str | None] = mapped_column(Text)
    price_source: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("tweet_id", "token_id", name="u_mention"),)


class TokenPrice(Base):
    __tablename__ = "token_prices"
    token_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tokens.id"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), primary_key=True)
    granularity: Mapped[str] = mapped_column(Text, primary_key=True)
    close_usd: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    source: Mapped[str | None] = mapped_column(Text)


class BenchmarkPrice(Base):
    __tablename__ = "benchmark_prices"
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), primary_key=True)
    close_usd: Mapped[Decimal] = mapped_column(Numeric(30, 10))


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    twitter_id: Mapped[str] = mapped_column(Text, unique=True)
    handle: Mapped[str] = mapped_column(Text)
    github_login: Mapped[str | None] = mapped_column(Text)
    joined_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    twitter_access_token: Mapped[str | None] = mapped_column(Text)
    twitter_refresh_token: Mapped[str | None] = mapped_column(Text)
    twitter_token_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class UserFollow(Base):
    __tablename__ = "user_follows"
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), primary_key=True
    )


class AccountCI(Base):
    __tablename__ = "account_ci"
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    median_excess: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    ci_low_excess: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    ci_high_excess: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    n_closed: Mapped[int | None] = mapped_column(Integer)
    computed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class RawTweet(Base):
    __tablename__ = "raw_tweets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tweet_id: Mapped[str] = mapped_column(Text, unique=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    tweet_ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    tweet_text: Mapped[str] = mapped_column(Text)
    raw_json: Mapped[dict] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    resolve_attempts: Mapped[int] = mapped_column(SmallInteger, default=0)
    resolve_last_error: Mapped[str | None] = mapped_column(Text)

"""shillscore CLI — `python -m shillscore <cmd>`.

Subcommands:
- seed --user <handle>   Enqueue a full sync for a user that has already authed
                         via /auth/twitter. Pulls follow list, then syncs each
                         account, then waits for an idle queue and prints the
                         leaderboard SQL output.

Run inside the api container so DATABASE_URL/REDIS_URL are set:
    docker compose -f infra/docker-compose.yml exec api python -m shillscore seed --user theogonella
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import select, text

from app.config import get_settings
from app.db import SessionLocal
from app.models import User


async def _cmd_seed(handle: str) -> int:
    settings = get_settings()
    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.handle == handle))
        ).scalar_one_or_none()
        if not user or not user.twitter_access_token:
            print(
                f"error: user @{handle} has not authed yet. Visit "
                f"https://{settings.public_hostname}/auth/twitter first.",
                file=sys.stderr,
            )
            return 1

    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    job = await redis.enqueue_job("sync_user_following")
    print(f"enqueued sync_user_following → job_id={job.job_id}")
    print("worker will fan out per-account syncs and per-mention price fetches.")
    print()
    print("Wait for the queue to drain, then run:")
    print("  make psql")
    print("  REFRESH MATERIALIZED VIEW mention_returns;")
    print("  REFRESH MATERIALIZED VIEW account_leaderboard;")
    print("  SELECT handle, n_closed, median_excess FROM account_leaderboard")
    print("  ORDER BY median_excess DESC NULLS LAST LIMIT 25;")
    return 0


async def _cmd_status() -> int:
    async with SessionLocal() as session:
        accounts = (await session.execute(text("SELECT count(*) FROM accounts"))).scalar_one()
        mentions = (await session.execute(text("SELECT count(*) FROM mentions"))).scalar_one()
        anchored = (
            await session.execute(
                text("SELECT count(*) FROM mentions WHERE price_at_mention IS NOT NULL")
            )
        ).scalar_one()
        tokens = (await session.execute(text("SELECT count(*) FROM tokens"))).scalar_one()
    print(f"accounts: {accounts}")
    print(f"tokens:   {tokens}")
    print(f"mentions: {mentions} (anchored: {anchored})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shillscore")
    sub = parser.add_subparsers(dest="cmd", required=True)

    seed = sub.add_parser("seed", help="enqueue a full sync for an authed user")
    seed.add_argument("--user", required=True, help="twitter handle, no @")

    sub.add_parser("status", help="print row counts for the core tables")

    args = parser.parse_args(argv)
    if args.cmd == "seed":
        return asyncio.run(_cmd_seed(args.user))
    if args.cmd == "status":
        return asyncio.run(_cmd_status())
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

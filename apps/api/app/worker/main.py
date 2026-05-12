"""arq worker entrypoint. Run with: python -m arq app.worker.main.WorkerSettings"""
from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.worker import jobs

_settings = get_settings()


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(_settings.redis_url)


class WorkerSettings:
    redis_settings = _redis_settings()
    functions = [
        jobs.sync_user_following,
        jobs.sync_batch,
        jobs.resolve_pending_tweets,
        jobs.resolve_pending_sweep,
        jobs.resolve_ambiguous_via_aliases,
        jobs.on_new_mention,
        jobs.anchor_all_pending,
        jobs.extend_token_prices_daily,
        jobs.refresh_benchmark_prices,
        jobs.refresh_mention_returns,
        jobs.bootstrap_account_ci,
        jobs.consider_deepening,
    ]
    # All times UTC. Daily ops between 06:00 and 06:30; sweeper runs 2×/hour.
    # `resolve_ambiguous_via_aliases` runs hourly so new aliases learned from
    # contract-bearing tweets retroactively repair earlier ambiguous mentions.
    cron_jobs = [
        cron(jobs.refresh_benchmark_prices, hour=6, minute=0, run_at_startup=False),
        cron(jobs.extend_token_prices_daily, hour=6, minute=10, run_at_startup=False),
        cron(jobs.refresh_mention_returns, hour=6, minute=20, run_at_startup=False),
        cron(jobs.bootstrap_account_ci, hour=6, minute=30, run_at_startup=False),
        cron(jobs.resolve_pending_sweep, minute={5, 35}, run_at_startup=False),
        cron(jobs.resolve_ambiguous_via_aliases, minute=45, run_at_startup=False),
    ]
    job_timeout = 1200  # seconds — bumped from 600 after anchor_all_pending hit
    # the timeout twice during the 2026-05-11 bulk rerun; per-mention ops are
    # still sub-second, this only matters for cold-start bulk sweeps.
    # X /tweets/search/all is 1 req/sec; client also throttles, but keeping
    # max_jobs=1 avoids head-of-line stalls and makes log reading simpler.
    max_jobs = 1

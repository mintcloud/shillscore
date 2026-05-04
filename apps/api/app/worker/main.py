"""arq worker entrypoint. Run with: python -m arq app.worker.main.WorkerSettings"""
from arq.connections import RedisSettings

from app.config import get_settings
from app.worker import jobs

_settings = get_settings()


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(_settings.redis_url)


class WorkerSettings:
    redis_settings = _redis_settings()
    functions = [
        jobs.sync_account,
        jobs.on_new_mention,
        jobs.extend_token_prices_daily,
        jobs.refresh_benchmark_prices,
        jobs.refresh_mention_returns,
        jobs.bootstrap_account_ci,
        jobs.consider_deepening,
    ]
    cron_jobs: list = []  # populated in Phase 1

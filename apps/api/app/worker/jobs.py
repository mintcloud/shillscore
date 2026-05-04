"""Worker jobs — stubs for Phase 0. Real implementations land in Phase 1.

Each function signature matches the catalogue in docs/plan.md §1.
"""
from typing import Any


async def sync_account(
    ctx: dict[str, Any],
    handle: str,
    since_id: str | None = None,
    lookback_days: int = 90,
) -> dict[str, Any]:
    return {"handle": handle, "since_id": since_id, "lookback_days": lookback_days, "stub": True}


async def on_new_mention(ctx: dict[str, Any], mention_id: int) -> dict[str, Any]:
    """Priority-queued price-window fetch. Fresh (<23h) -> 5min ±2h; aged -> hourly ±24h."""
    return {"mention_id": mention_id, "stub": True}


async def extend_token_prices_daily(ctx: dict[str, Any], token_id: int) -> dict[str, Any]:
    return {"token_id": token_id, "stub": True}


async def refresh_benchmark_prices(ctx: dict[str, Any]) -> dict[str, Any]:
    return {"stub": True}


async def refresh_mention_returns(ctx: dict[str, Any]) -> dict[str, Any]:
    return {"stub": True}


async def bootstrap_account_ci(ctx: dict[str, Any]) -> dict[str, Any]:
    return {"stub": True}


async def consider_deepening(ctx: dict[str, Any], account_id: int) -> dict[str, Any]:
    return {"account_id": account_id, "stub": True}

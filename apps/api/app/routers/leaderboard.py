from fastapi import APIRouter

router = APIRouter(tags=["leaderboard"])


@router.get("/leaderboard")
async def get_leaderboard(cohort: str = "365d", sort: str = "excess") -> dict[str, list]:
    """Stub. Phase 2 reads from the `account_leaderboard` materialized view."""
    return {"rows": []}


@router.get("/account/{handle}")
async def get_account(handle: str) -> dict[str, object]:
    """Stub. Phase 2 returns account profile + every mention with chart series."""
    return {"handle": handle, "mentions": []}


@router.get("/mention/{mention_id}/series")
async def get_mention_series(mention_id: int) -> dict[str, object]:
    """Stub. Phase 2 returns mixed-granularity price series for the chart."""
    return {"mention_id": mention_id, "points": []}

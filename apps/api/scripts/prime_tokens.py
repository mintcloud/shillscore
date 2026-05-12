"""Pre-prime the tokens table from CoinGecko's top-N by market cap.

Why: the resolver's Tier 1 (DB hit by symbol with NULL contract_addr) is
the hot path. With the top-1000 already in the DB, ~95% of cashtag mentions
resolve without a /search call.

Ambiguity caveat: multiple top-1000 coins share symbols (e.g. MEGA).
Tier 1 uses `scalar_one_or_none()` and will error if two rows match. We
dedupe before insert, keeping the *higher-mcap* (smaller rank) entry —
that's the global default; per-account aliasing (Tier 3a) overrides.

Run via:
    docker compose -f infra/docker-compose.yml --env-file infra/.env \
        exec api python -m scripts.prime_tokens
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.clients import coingecko
from app.db import SessionLocal
from app.models import Token


async def main(limit: int = 1000) -> None:
    coins = await coingecko.top_markets(limit)
    # Sort by mcap rank ascending (best first), then dedupe by symbol.
    coins = sorted(coins, key=lambda c: c.get("market_cap_rank") or 10**9)
    seen: set[str] = set()
    deduped: list[dict] = []
    for c in coins:
        sym = (c.get("symbol") or "").upper().strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        deduped.append(c)

    async with SessionLocal() as session:
        existing_cg_ids = set(
            (await session.execute(select(Token.coingecko_id))).scalars().all()
        )
        existing_syms = set(
            (
                await session.execute(
                    select(Token.symbol).where(Token.contract_addr.is_(None))
                )
            ).scalars().all()
        )

        inserted = 0
        skipped_existing_id = 0
        skipped_existing_symbol = 0
        for c in deduped:
            cg_id = c.get("id")
            sym = (c.get("symbol") or "").upper().strip()
            if not cg_id or not sym:
                continue
            if cg_id in existing_cg_ids:
                skipped_existing_id += 1
                continue
            if sym in existing_syms:
                # A ticker-resolved row already exists with this symbol —
                # could be from contract-derived alias or a prior prime.
                # Skip so Tier 1's scalar_one_or_none() stays valid.
                skipped_existing_symbol += 1
                continue
            session.add(
                Token(
                    coingecko_id=cg_id,
                    symbol=sym,
                    name=c.get("name"),
                    contract_addr=None,
                    chain=None,
                    is_verified=True,
                )
            )
            existing_cg_ids.add(cg_id)
            existing_syms.add(sym)
            inserted += 1
        await session.commit()

    print(
        f"prime_tokens: fetched={len(coins)} unique_after_dedupe={len(deduped)} "
        f"inserted={inserted} skipped_existing_id={skipped_existing_id} "
        f"skipped_existing_symbol={skipped_existing_symbol}"
    )


if __name__ == "__main__":
    asyncio.run(main())

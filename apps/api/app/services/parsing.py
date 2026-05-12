"""Token mention extraction from raw tweet text.

Two patterns (per plan §6):
- Contract address (HIGH confidence): EVM `0x[a-fA-F0-9]{40}`, Solana base58 32-44.
- $TICKER (MEDIUM): cashtag, 2-10 ascii letters/digits.

`extract_from_tweet` is the preferred entrypoint: it picks the full body
(`note_tweet.text` over truncated `text`) and unions Twitter's parsed
`entities.cashtags` with our regex output to recover cashtags that the
280-char `text` cut off.

Returns structured matches; resolution to a `tokens` row happens in resolver.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

EVM_ADDR_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
# Solana: base58 (no 0,O,I,l), 32-44 chars. Tighter than EVM, false positives possible.
SOL_ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")


@dataclass(frozen=True)
class TokenMatch:
    raw: str
    kind: str  # "contract" | "ticker"
    chain: str | None = None  # for contracts: "ethereum" | "solana" | ...

    def normalized(self) -> str:
        if self.kind == "ticker":
            # The `$` is a Twitter cashtag prefix, not part of the symbol. Strip
            # it before any DB lookup or CG search — otherwise the symbol-exact
            # filter on /search matches only scam tokens that literally use `$X`
            # as their CG symbol (e.g. `freetrump` symbol=`$TRUMP`).
            return self.raw.lstrip("$").upper()
        return self.raw.lower() if self.chain == "ethereum" else self.raw


def extract_matches(text: str) -> list[TokenMatch]:
    """Best-effort parse. Order: contracts first; tickers only kept if no contract in tweet.

    Plan §6: 'If the same tweet has a contract address → use that, ignore the ticker.'
    """
    matches: list[TokenMatch] = []
    seen: set[tuple[str, str]] = set()

    for m in EVM_ADDR_RE.finditer(text):
        key = ("contract", m.group(0).lower())
        if key not in seen:
            matches.append(TokenMatch(raw=m.group(0), kind="contract", chain="ethereum"))
            seen.add(key)

    if not matches:
        # Solana addresses are looser; only consider when there's no EVM contract.
        # Heuristic: must NOT contain a bare alpha word, must look base58. Skip if
        # it's just a normal English word inside the regex range.
        for m in SOL_ADDR_RE.finditer(text):
            tok = m.group(0)
            if len(tok) >= 32 and any(c.isdigit() for c in tok):
                key = ("contract", tok)
                if key not in seen:
                    matches.append(TokenMatch(raw=tok, kind="contract", chain="solana"))
                    seen.add(key)

    if not any(m.kind == "contract" for m in matches):
        for m in TICKER_RE.finditer(text):
            sym = m.group(1).upper()
            key = ("ticker", sym)
            if key not in seen:
                matches.append(TokenMatch(raw=m.group(0), kind="ticker"))
                seen.add(key)

    return matches


_TICKER_SHAPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,9}$")


def extract_from_tweet(tweet: dict[str, Any]) -> list[TokenMatch]:
    """Tweet-level extraction: full body + Twitter's parsed cashtags.

    - Body source: `note_tweet.text` if present (long-form tweets), else `text`.
      `text` is truncated at ~280 chars with an ellipsis; cashtags past the
      cutoff would otherwise be lost.
    - Cashtag union: every symbol Twitter parsed into `entities.cashtags` is
      added as a ticker match, even if the regex missed it (e.g. it sat past
      the truncation, or the tweet only stored the cashtag in entities for
      a quoted/retweeted body).
    """
    note = (tweet.get("note_tweet") or {}).get("text")
    body = note or tweet.get("text") or ""
    matches = extract_matches(body)

    # If a contract was matched, we keep the contract-only behaviour from
    # extract_matches (ignore tickers in the same tweet).
    if any(m.kind == "contract" for m in matches):
        return matches

    seen_tickers: set[str] = {m.normalized() for m in matches if m.kind == "ticker"}
    cashtags = (tweet.get("entities") or {}).get("cashtags") or []
    for entry in cashtags:
        tag = (entry.get("tag") or "").upper()
        if not tag or not _TICKER_SHAPE_RE.match(tag):
            continue
        if tag in seen_tickers:
            continue
        matches.append(TokenMatch(raw=f"${tag}", kind="ticker"))
        seen_tickers.add(tag)

    return matches


def extract_cashtags_only(tweet: dict[str, Any]) -> list[str]:
    """Return cashtag symbols found in the tweet body + Twitter entities, upper-cased.

    Used by alias inference: when a tweet resolves via contract address, we
    still want to know which `$TICKER` symbols appeared in the same text so
    we can register them as aliases for the contract's token.
    """
    note = (tweet.get("note_tweet") or {}).get("text")
    body = note or tweet.get("text") or ""
    tags: set[str] = set()
    for m in TICKER_RE.finditer(body):
        tags.add(m.group(1).upper())
    for entry in (tweet.get("entities") or {}).get("cashtags") or []:
        tag = (entry.get("tag") or "").upper()
        if tag and _TICKER_SHAPE_RE.match(tag):
            tags.add(tag)
    return sorted(tags)

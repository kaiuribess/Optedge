"""Resolve user search text into a tradable ticker/symbol using free sources."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}(=F)?$")
_OCCISH_RE = re.compile(
    r"^(?P<ticker>[A-Z]{1,6})\s+"
    r"(?P<expiry>(?:20)?\d{2}[-/]?\d{2}[-/]?\d{2}|\d{6,8})\s+"
    r"(?P<side>[CP]|CALL|PUT)\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)


@dataclass
class Resolution:
    query: str
    symbol: str | None
    name: str | None = None
    source: str = "none"
    candidates: list[dict[str, Any]] | None = None
    request: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_query(query: str) -> str:
    return str(query or "").strip()


def _normalize_expiry(value: str) -> str:
    raw = re.sub(r"[^0-9]", "", str(value or ""))
    if len(raw) == 6:
        raw = "20" + raw
    if len(raw) == 8:
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return str(value or "").strip()


def parse_option_request(query: str) -> dict[str, Any] | None:
    q = _clean_query(query).upper()
    match = _OCCISH_RE.match(q)
    if not match:
        return None
    side_raw = match.group("side").upper()
    return {
        "asset": "option",
        "ticker": match.group("ticker").upper(),
        "expiry": _normalize_expiry(match.group("expiry")),
        "side": "call" if side_raw in {"C", "CALL"} else "put",
        "strike": float(match.group("strike")),
        "raw": _clean_query(query),
    }


def _direct_symbol(query: str) -> str | None:
    raw = _clean_query(query)
    q = raw.upper()
    if not q:
        return None
    option_request = parse_option_request(q)
    if option_request:
        return str(option_request["ticker"]).upper()
    if _SYMBOL_RE.match(q):
        if q.endswith("=F") or "." in q or "-" in q:
            return q
        if len(q) <= 5:
            return q
        return None
    return None

def _candidate_from_quote(quote: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(quote.get("symbol") or "").strip().upper()
    if not symbol or len(symbol) > 12:
        return None
    quote_type = str(quote.get("quoteType") or quote.get("typeDisp") or "").upper()
    if quote_type and quote_type not in {"EQUITY", "ETF", "MUTUALFUND", "INDEX", "FUTURE"}:
        return None
    return {
        "symbol": symbol,
        "name": quote.get("shortname") or quote.get("longname") or quote.get("name"),
        "exchange": quote.get("exchange") or quote.get("exchDisp"),
        "type": quote_type or quote.get("typeDisp"),
        "score": quote.get("score"),
    }


def yahoo_search(query: str, limit: int = 8, timeout: float = 6.0) -> list[dict[str, Any]]:
    params = urlencode({"q": query, "quotesCount": limit, "newsCount": 0})
    url = f"https://query1.finance.yahoo.com/v1/finance/search?{params}"
    req = Request(url, headers={"User-Agent": "optedge-research/0.1"})
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    quotes = data.get("quotes") if isinstance(data, dict) else []
    candidates: list[dict[str, Any]] = []
    for quote in quotes or []:
        candidate = _candidate_from_quote(quote)
        if candidate and candidate["symbol"] not in {c["symbol"] for c in candidates}:
            candidates.append(candidate)
    return candidates


def resolve_symbol(query: str, timeout: float = 6.0) -> dict[str, Any]:
    clean = _clean_query(query)
    option_request = parse_option_request(clean)
    direct = _direct_symbol(clean)
    if direct:
        return Resolution(query=clean, symbol=direct, source="direct", candidates=[],
                          request=option_request).to_dict()
    if not clean:
        return Resolution(query=clean, symbol=None, error="empty query").to_dict()
    try:
        candidates = yahoo_search(clean, timeout=timeout)
    except Exception as exc:
        return Resolution(query=clean, symbol=None, source="yahoo", error=str(exc)).to_dict()
    if not candidates:
        return Resolution(query=clean, symbol=None, source="yahoo", candidates=[],
                          error="no symbol candidates found").to_dict()
    best = candidates[0]
    return Resolution(
        query=clean,
        symbol=best["symbol"],
        name=best.get("name"),
        source="yahoo",
        candidates=candidates,
    ).to_dict()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Resolve company/ticker text to a symbol.")
    parser.add_argument("query")
    args = parser.parse_args()
    print(json.dumps(resolve_symbol(args.query), indent=2))

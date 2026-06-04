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

_ALIAS_CLEAN_RE = re.compile(r"[^a-z0-9]+")

COMMON_ALIASES: dict[str, tuple[str, str]] = {
    # Mega-cap / high-attention equities
    "apple": ("AAPL", "Apple Inc."),
    "apple inc": ("AAPL", "Apple Inc."),
    "nvidia": ("NVDA", "NVIDIA Corporation"),
    "nvidia corporation": ("NVDA", "NVIDIA Corporation"),
    "tesla": ("TSLA", "Tesla, Inc."),
    "tesla inc": ("TSLA", "Tesla, Inc."),
    "microsoft": ("MSFT", "Microsoft Corporation"),
    "microsoft corporation": ("MSFT", "Microsoft Corporation"),
    "amazon": ("AMZN", "Amazon.com, Inc."),
    "amazon com": ("AMZN", "Amazon.com, Inc."),
    "meta": ("META", "Meta Platforms, Inc."),
    "facebook": ("META", "Meta Platforms, Inc."),
    "google": ("GOOGL", "Alphabet Inc."),
    "alphabet": ("GOOGL", "Alphabet Inc."),
    "amd": ("AMD", "Advanced Micro Devices, Inc."),
    "advanced micro devices": ("AMD", "Advanced Micro Devices, Inc."),
    "palantir": ("PLTR", "Palantir Technologies Inc."),
    "coinbase": ("COIN", "Coinbase Global, Inc."),
    "microstrategy": ("MSTR", "Strategy Inc."),
    "strategy": ("MSTR", "Strategy Inc."),
    "super micro": ("SMCI", "Super Micro Computer, Inc."),
    "supermicro": ("SMCI", "Super Micro Computer, Inc."),
    "broadcom": ("AVGO", "Broadcom Inc."),
    "netflix": ("NFLX", "Netflix, Inc."),
    "disney": ("DIS", "The Walt Disney Company"),
    "walmart": ("WMT", "Walmart Inc."),
    "berkshire": ("BRK-B", "Berkshire Hathaway Inc. Class B"),
    "berkshire hathaway": ("BRK-B", "Berkshire Hathaway Inc. Class B"),
    "jp morgan": ("JPM", "JPMorgan Chase & Co."),
    "jpmorgan": ("JPM", "JPMorgan Chase & Co."),
    "gamestop": ("GME", "GameStop Corp."),
    "game stop": ("GME", "GameStop Corp."),
    "amc": ("AMC", "AMC Entertainment Holdings, Inc."),
    "sofi": ("SOFI", "SoFi Technologies, Inc."),
    "rivian": ("RIVN", "Rivian Automotive, Inc."),
    "lucid": ("LCID", "Lucid Group, Inc."),
    "robinhood": ("HOOD", "Robinhood Markets, Inc."),
    "draftkings": ("DKNG", "DraftKings Inc."),
    "blackberry": ("BB", "BlackBerry Limited"),
    "rocket lab": ("RKLB", "Rocket Lab USA, Inc."),
    "asts": ("ASTS", "AST SpaceMobile, Inc."),
    "ast spacemobile": ("ASTS", "AST SpaceMobile, Inc."),
    "ast space mobile": ("ASTS", "AST SpaceMobile, Inc."),
    "ionq": ("IONQ", "IonQ, Inc."),
    "rigetti": ("RGTI", "Rigetti Computing, Inc."),

    # Equity/index ETFs users often mean by plain-language searches
    "spy": ("SPY", "SPDR S&P 500 ETF Trust"),
    "s p 500 etf": ("SPY", "SPDR S&P 500 ETF Trust"),
    "s&p 500 etf": ("SPY", "SPDR S&P 500 ETF Trust"),
    "qqq": ("QQQ", "Invesco QQQ Trust"),
    "nasdaq etf": ("QQQ", "Invesco QQQ Trust"),
    "russell 2000 etf": ("IWM", "iShares Russell 2000 ETF"),
    "bitcoin etf": ("IBIT", "iShares Bitcoin Trust ETF"),
    "ethereum etf": ("ETHA", "iShares Ethereum Trust ETF"),

    # Futures symbols used by Optedge
    "s p 500 futures": ("ES=F", "S&P 500 E-mini Futures"),
    "s&p 500 futures": ("ES=F", "S&P 500 E-mini Futures"),
    "sp500 futures": ("ES=F", "S&P 500 E-mini Futures"),
    "es futures": ("ES=F", "S&P 500 E-mini Futures"),
    "nasdaq futures": ("NQ=F", "Nasdaq-100 E-mini Futures"),
    "nasdaq 100 futures": ("NQ=F", "Nasdaq-100 E-mini Futures"),
    "nq futures": ("NQ=F", "Nasdaq-100 E-mini Futures"),
    "dow futures": ("YM=F", "Dow E-mini Futures"),
    "russell futures": ("RTY=F", "Russell 2000 E-mini Futures"),
    "vix": ("^VIX", "CBOE Volatility Index"),
    "volatility index": ("^VIX", "CBOE Volatility Index"),
    "crude oil": ("CL=F", "Crude Oil WTI Futures"),
    "oil futures": ("CL=F", "Crude Oil WTI Futures"),
    "wti": ("CL=F", "Crude Oil WTI Futures"),
    "natural gas": ("NG=F", "Natural Gas Futures"),
    "nat gas": ("NG=F", "Natural Gas Futures"),
    "gold": ("GC=F", "Gold Futures"),
    "gold futures": ("GC=F", "Gold Futures"),
    "silver": ("SI=F", "Silver Futures"),
    "silver futures": ("SI=F", "Silver Futures"),
    "copper": ("HG=F", "Copper Futures"),
    "wheat": ("ZW=F", "Wheat Futures"),
    "corn": ("ZC=F", "Corn Futures"),
    "soybeans": ("ZS=F", "Soybean Futures"),
    "dollar index": ("DX=F", "US Dollar Index Futures"),
    "bitcoin futures": ("BTC=F", "Bitcoin Futures"),
    "ether futures": ("ETH=F", "Ether Futures"),
    "ethereum futures": ("ETH=F", "Ether Futures"),
}


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


def _alias_key(query: str) -> str:
    cleaned = _ALIAS_CLEAN_RE.sub(" ", str(query or "").lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def _alias_match(query: str) -> tuple[str, str] | None:
    key = _alias_key(query)
    if not key:
        return None
    return COMMON_ALIASES.get(key)


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
    raw_ticker = match.group("ticker").upper()
    alias = _alias_match(raw_ticker)
    return {
        "asset": "option",
        "ticker": alias[0] if alias else raw_ticker,
        "expiry": _normalize_expiry(match.group("expiry")),
        "side": "call" if side_raw in {"C", "CALL"} else "put",
        "strike": float(match.group("strike")),
        "raw": _clean_query(query),
        "ticker_source": "alias" if alias else "direct",
        "ticker_name": alias[1] if alias else None,
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
    alias = _alias_match(str(option_request.get("ticker")) if option_request else clean)
    if alias:
        symbol, name = alias
        if option_request:
            option_request["ticker"] = symbol
        return Resolution(query=clean, symbol=symbol, name=name, source="alias",
                          candidates=[{"symbol": symbol, "name": name, "type": "ALIAS"}],
                          request=option_request).to_dict()
    if option_request and option_request.get("ticker_source") == "alias":
        symbol = str(option_request.get("ticker") or "")
        name = option_request.get("ticker_name")
        return Resolution(query=clean, symbol=symbol, name=name, source="alias",
                          candidates=[{"symbol": symbol, "name": name, "type": "ALIAS"}],
                          request=option_request).to_dict()
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

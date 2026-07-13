# Purpose: Resolve company names and aliases to tradable symbols.
"""Resolve user search text into a tradable ticker/symbol using free sources."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from optedge.http_identity import SecContactRequiredError, outbound_headers, sec_headers


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_TICKER_CACHE = DATA_DIR / "sec_company_tickers.json"
SEC_CACHE_MAX_AGE_DAYS = 14
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
NASDAQ_OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
NASDAQ_SYMBOL_CACHE = DATA_DIR / "nasdaq_symbol_directory.json"
NASDAQ_SYMBOL_CACHE_MAX_AGE_DAYS = 3

log = logging.getLogger("optedge.symbol_resolver")

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}(=F)?$")
_OCCISH_RE = re.compile(
    r"^(?P<underlying>[A-Z][A-Z0-9 .&,\-]{0,80}?)\s+"
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


def _company_key(query: str) -> str:
    key = _alias_key(query)
    suffixes = {
        "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
        "plc", "class", "common", "stock", "the", "com",
    }
    words = [w for w in key.split() if w and w not in suffixes]
    return " ".join(words)


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
    raw_underlying = match.group("underlying").strip()
    raw_upper = raw_underlying.upper()
    alias = _alias_match(raw_underlying)
    is_direct = bool(_SYMBOL_RE.match(raw_upper) and (raw_upper.endswith("=F") or len(raw_upper) <= 5))
    return {
        "asset": "option",
        "ticker": alias[0] if alias else raw_upper,
        "expiry": _normalize_expiry(match.group("expiry")),
        "side": "call" if side_raw in {"C", "CALL"} else "put",
        "strike": float(match.group("strike")),
        "raw": _clean_query(query),
        "ticker_source": "alias" if alias else "direct" if is_direct else "unresolved_name",
        "ticker_name": alias[1] if alias else raw_underlying.title() if not is_direct else None,
    }


def _direct_symbol(query: str) -> str | None:
    raw = _clean_query(query)
    q = raw.upper()
    if not q:
        return None
    option_request = parse_option_request(q)
    if option_request:
        if option_request.get("ticker_source") == "unresolved_name":
            return None
        return str(option_request["ticker"]).upper()
    if _SYMBOL_RE.match(q):
        if q.endswith("=F") or "." in q or "-" in q:
            return q
        if len(q) <= 5:
            return q
        return None
    return None


def _cache_age_days(path: Path) -> float | None:
    try:
        return (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 86400.0
    except Exception:
        return None


def sec_company_cache_meta(cache_path: Path | None = None) -> dict[str, Any]:
    """Return local SEC ticker-cache health without fetching the network."""
    path = Path(cache_path or SEC_TICKER_CACHE)
    age_days = _cache_age_days(path)
    if age_days is None:
        return {
            "exists": False,
            "path": str(path),
            "age_days": None,
            "status": "missing",
            "row_count": 0,
        }
    row_count = 0
    try:
        cached = json.loads(path.read_text(encoding="utf-8-sig"))
        row_count = len(_normalize_sec_rows(cached.get("rows", cached) if isinstance(cached, dict) else cached))
    except Exception:
        return {
            "exists": True,
            "path": str(path),
            "age_days": round(age_days, 2),
            "status": "corrupt",
            "row_count": 0,
        }
    status = "fresh" if age_days <= SEC_CACHE_MAX_AGE_DAYS else "stale"
    return {
        "exists": True,
        "path": str(path),
        "age_days": round(age_days, 2),
        "status": status,
        "row_count": row_count,
    }


def _normalize_sec_rows(raw: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    iterable = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    seen: set[str] = set()
    for item in iterable:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("ticker") or item.get("symbol") or "").strip().upper()
        title = str(item.get("title") or item.get("name") or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        rows.append({
            "symbol": symbol,
            "name": title,
            "cik": item.get("cik_str") or item.get("cik"),
            "exchange": item.get("exchange"),
            "type": "EQUITY",
            "source": "sec_company_tickers",
        })
    return rows


def _nasdaq_exchange_name(code: str) -> str:
    return {
        "Q": "NASDAQ Global Select",
        "G": "NASDAQ Global Market",
        "S": "NASDAQ Capital Market",
        "N": "NYSE",
        "A": "NYSE American",
        "P": "NYSE Arca",
        "Z": "Cboe BZX",
        "V": "Investors Exchange",
    }.get(str(code or "").strip().upper(), str(code or "").strip().upper())


def _nasdaq_app_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip().upper()
    return text.replace("/", "-").replace("$", "-")


def _nasdaq_security_type(symbol: str, name: str, etf_flag: str) -> str:
    if str(etf_flag or "").strip().upper() == "Y":
        return "ETF"
    low = f"{symbol} {name}".lower()
    if "warrant" in low or symbol.endswith("+") or symbol.endswith(".WS"):
        return "WARRANT"
    if " unit" in low or symbol.endswith("=") or symbol.endswith(".U"):
        return "UNIT"
    if " right" in low or symbol.endswith("^") or symbol.endswith(".R"):
        return "RIGHT"
    if "preferred" in low or "-P" in symbol:
        return "PREFERRED"
    return "EQUITY"


def _parse_nasdaq_symbol_text(text: str, source: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].split("|")
    rows: list[dict[str, Any]] = []
    for line in lines[1:]:
        if line.lower().startswith("file creation time"):
            continue
        parts = line.split("|")
        if len(parts) != len(header):
            continue
        raw = dict(zip(header, parts))
        if source == "nasdaq":
            symbol = _nasdaq_app_symbol(raw.get("Symbol"))
            name = str(raw.get("Security Name") or "").strip()
            exchange = _nasdaq_exchange_name(raw.get("Market Category") or "NASDAQ")
            raw_exchange = str(raw.get("Market Category") or "").strip()
            etf = str(raw.get("ETF") or "").strip().upper() == "Y"
            test_issue = str(raw.get("Test Issue") or "").strip().upper() == "Y"
            round_lot = raw.get("Round Lot Size")
        else:
            symbol = _nasdaq_app_symbol(raw.get("NASDAQ Symbol") or raw.get("ACT Symbol"))
            name = str(raw.get("Security Name") or "").strip()
            exchange = _nasdaq_exchange_name(raw.get("Exchange") or "")
            raw_exchange = str(raw.get("Exchange") or "").strip().upper()
            etf = str(raw.get("ETF") or "").strip().upper() == "Y"
            test_issue = str(raw.get("Test Issue") or "").strip().upper() == "Y"
            round_lot = raw.get("Round Lot Size")
        if not symbol or not name:
            continue
        sec_type = _nasdaq_security_type(symbol, name, "Y" if etf else "N")
        rows.append({
            "symbol": symbol,
            "name": name,
            "exchange": exchange,
            "exchange_code": raw_exchange,
            "type": sec_type,
            "is_etf": etf,
            "test_issue": test_issue,
            "round_lot_size": round_lot,
            "source": "nasdaq_symbol_directory",
        })
    return rows


def _normalize_nasdaq_rows(raw: Any) -> list[dict[str, Any]]:
    iterable = raw.get("rows", raw) if isinstance(raw, dict) else raw
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(iterable, list):
        return rows
    for item in iterable:
        if not isinstance(item, dict):
            continue
        symbol = _nasdaq_app_symbol(item.get("symbol") or item.get("ticker"))
        name = str(item.get("name") or item.get("Security Name") or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        rows.append({
            "symbol": symbol,
            "name": name,
            "exchange": item.get("exchange"),
            "exchange_code": item.get("exchange_code"),
            "type": item.get("type") or "EQUITY",
            "is_etf": bool(item.get("is_etf")),
            "test_issue": bool(item.get("test_issue")),
            "round_lot_size": item.get("round_lot_size"),
            "source": "nasdaq_symbol_directory",
        })
    return rows


def nasdaq_symbol_cache_meta(cache_path: Path | None = None) -> dict[str, Any]:
    path = Path(cache_path or NASDAQ_SYMBOL_CACHE)
    age_days = _cache_age_days(path)
    if age_days is None:
        return {
            "exists": False,
            "path": str(path),
            "age_days": None,
            "status": "missing",
            "row_count": 0,
        }
    try:
        cached = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = _normalize_nasdaq_rows(cached)
    except Exception:
        return {
            "exists": True,
            "path": str(path),
            "age_days": round(age_days, 2),
            "status": "corrupt",
            "row_count": 0,
        }
    status = "fresh" if age_days <= NASDAQ_SYMBOL_CACHE_MAX_AGE_DAYS else "stale"
    return {
        "exists": True,
        "path": str(path),
        "age_days": round(age_days, 2),
        "status": status,
        "row_count": len(rows),
    }


def fetch_nasdaq_symbol_directory(timeout: float = 8.0) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for url, source in (
        (NASDAQ_LISTED_URL, "nasdaq"),
        (NASDAQ_OTHER_LISTED_URL, "other"),
    ):
        req = Request(url, headers=outbound_headers())
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        rows.extend(_parse_nasdaq_symbol_text(text, source))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        deduped.append(row)
    return deduped


def load_nasdaq_symbol_directory(
    cache_path: Path | None = None,
    max_age_days: int = NASDAQ_SYMBOL_CACHE_MAX_AGE_DAYS,
    timeout: float = 8.0,
    fetch_if_stale: bool = True,
) -> list[dict[str, Any]]:
    cache_path = Path(cache_path or NASDAQ_SYMBOL_CACHE)
    age_days = _cache_age_days(cache_path)
    if age_days is not None and age_days <= max_age_days:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            return _normalize_nasdaq_rows(cached)
        except Exception:
            pass

    if fetch_if_stale:
        try:
            rows = fetch_nasdaq_symbol_directory(timeout=timeout)
            if rows:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "source": [NASDAQ_LISTED_URL, NASDAQ_OTHER_LISTED_URL],
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "rows": rows,
                }
                cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                return rows
        except Exception:
            pass

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            return _normalize_nasdaq_rows(cached)
        except Exception:
            return []
    return []


def fetch_sec_company_tickers(timeout: float = 6.0) -> list[dict[str, Any]]:
    req = Request(
        SEC_TICKER_URL,
        headers=sec_headers(accept="application/json"),
    )
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    return _normalize_sec_rows(data)


def load_sec_company_tickers(
    cache_path: Path | None = None,
    max_age_days: int = SEC_CACHE_MAX_AGE_DAYS,
    timeout: float = 6.0,
    fetch_if_stale: bool = True,
) -> list[dict[str, Any]]:
    """Load the free SEC ticker map with a small local cache."""
    cache_path = Path(cache_path or SEC_TICKER_CACHE)
    age_days = _cache_age_days(cache_path)
    if age_days is not None and age_days <= max_age_days:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            return _normalize_sec_rows(cached.get("rows", cached) if isinstance(cached, dict) else cached)
        except Exception:
            pass

    if fetch_if_stale:
        try:
            rows = fetch_sec_company_tickers(timeout=timeout)
            if rows:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "source": SEC_TICKER_URL,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "rows": rows,
                }
                cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                return rows
        except SecContactRequiredError as exc:
            log.warning("SEC ticker refresh skipped: %s", exc)
        except Exception:
            pass

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            return _normalize_sec_rows(cached.get("rows", cached) if isinstance(cached, dict) else cached)
        except Exception:
            return []
    return []


def _sec_candidate_score(query: str, row: dict[str, Any]) -> float:
    q_symbol = _clean_query(query).upper()
    q_key = _company_key(query)
    symbol = str(row.get("symbol") or "").upper()
    name = str(row.get("name") or "")
    name_key = _company_key(name)
    if not q_key and not q_symbol:
        return 0.0
    if q_symbol == symbol:
        return 1.0
    if q_key and q_key == name_key:
        return 0.97
    if q_key and name_key.startswith(q_key):
        return 0.9
    q_words = set(q_key.split())
    name_words = set(name_key.split())
    if q_words and q_words.issubset(name_words):
        return 0.84
    if q_key and q_key in name_key:
        return 0.78
    return SequenceMatcher(None, q_key, name_key).ratio() * 0.7


def sec_company_search(
    query: str,
    limit: int = 8,
    timeout: float = 6.0,
    fetch_if_stale: bool = True,
) -> list[dict[str, Any]]:
    rows = load_sec_company_tickers(timeout=timeout, fetch_if_stale=fetch_if_stale)
    scored: list[dict[str, Any]] = []
    for row in rows:
        score = _sec_candidate_score(query, row)
        if score < 0.55:
            continue
        item = dict(row)
        item["score"] = round(score, 4)
        scored.append(item)
    scored.sort(key=lambda x: (float(x.get("score") or 0.0), str(x.get("symbol") or "")), reverse=True)
    return scored[:limit]


def _nasdaq_candidate_score(query: str, row: dict[str, Any]) -> float:
    q_symbol = _clean_query(query).upper()
    q_key = _company_key(query)
    symbol = str(row.get("symbol") or "").upper()
    name = str(row.get("name") or "")
    name_key = _company_key(name)
    if not q_key and not q_symbol:
        return 0.0
    if q_symbol == symbol:
        return 1.0
    if symbol.startswith(q_symbol) and len(q_symbol) >= 2:
        return 0.88
    if q_key and q_key == name_key:
        return 0.96
    if q_key and name_key.startswith(q_key):
        return 0.89
    q_words = set(q_key.split())
    name_words = set(name_key.split())
    if q_words and q_words.issubset(name_words):
        return 0.83
    if q_key and q_key in name_key:
        return 0.76
    score = SequenceMatcher(None, q_key, name_key).ratio() * 0.68
    if str(row.get("type") or "") not in {"EQUITY", "ETF"}:
        score *= 0.75
    return score


def nasdaq_symbol_search(
    query: str,
    limit: int = 8,
    timeout: float = 8.0,
    fetch_if_stale: bool = True,
) -> list[dict[str, Any]]:
    rows = load_nasdaq_symbol_directory(timeout=timeout, fetch_if_stale=fetch_if_stale)
    scored: list[dict[str, Any]] = []
    for row in rows:
        if row.get("test_issue"):
            continue
        score = _nasdaq_candidate_score(query, row)
        if score < 0.55:
            continue
        item = dict(row)
        item["score"] = round(score, 4)
        scored.append(item)
    scored.sort(
        key=lambda x: (
            str(x.get("type") or "") in {"EQUITY", "ETF"},
            float(x.get("score") or 0.0),
            str(x.get("symbol") or ""),
        ),
        reverse=True,
    )
    return scored[:limit]

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
    req = Request(url, headers=outbound_headers(accept="application/json"))
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
    search_text = clean
    if option_request and option_request.get("ticker_source") == "unresolved_name":
        search_text = str(option_request.get("ticker_name") or option_request.get("ticker") or clean)
    sec_candidates = sec_company_search(search_text, timeout=timeout)
    if sec_candidates:
        best = sec_candidates[0]
        if option_request:
            option_request["ticker"] = best["symbol"]
            option_request["ticker_source"] = "sec"
            option_request["ticker_name"] = best.get("name")
        return Resolution(
            query=clean,
            symbol=best["symbol"],
            name=best.get("name"),
            source="sec",
            candidates=sec_candidates,
            request=option_request,
        ).to_dict()
    nasdaq_candidates = nasdaq_symbol_search(search_text, timeout=timeout)
    if nasdaq_candidates:
        best = nasdaq_candidates[0]
        if option_request:
            option_request["ticker"] = best["symbol"]
            option_request["ticker_source"] = "nasdaq"
            option_request["ticker_name"] = best.get("name")
        return Resolution(
            query=clean,
            symbol=best["symbol"],
            name=best.get("name"),
            source="nasdaq",
            candidates=nasdaq_candidates,
            request=option_request,
        ).to_dict()
    try:
        candidates = yahoo_search(search_text, timeout=timeout)
    except Exception as exc:
        return Resolution(query=clean, symbol=None, source="yahoo", error=str(exc)).to_dict()
    if not candidates:
        return Resolution(query=clean, symbol=None, source="yahoo", candidates=[],
                          error="no symbol candidates found").to_dict()
    best = candidates[0]
    if option_request:
        option_request["ticker"] = best["symbol"]
        option_request["ticker_source"] = "yahoo"
        option_request["ticker_name"] = best.get("name")
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

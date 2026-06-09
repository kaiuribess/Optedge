import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.symbol_resolver as resolver


def resolve_symbol(query):
    return resolver.resolve_symbol(query)


def test_resolver_accepts_direct_ticker():
    res = resolve_symbol("nvda")
    assert res["symbol"] == "NVDA"
    assert res["source"] == "direct"


def test_resolver_extracts_underlying_from_option_text():
    res = resolve_symbol("AAPL 20260618 C 200")
    assert res["symbol"] == "AAPL"
    assert res["source"] == "direct"
    assert res["request"] == {
        "asset": "option",
        "ticker": "AAPL",
        "expiry": "2026-06-18",
        "side": "call",
        "strike": 200.0,
        "raw": "AAPL 20260618 C 200",
        "ticker_source": "direct",
        "ticker_name": None,
    }


def test_resolver_reports_empty_query():
    res = resolve_symbol("")
    assert res["symbol"] is None
    assert res["error"] == "empty query"


def test_resolver_uses_yahoo_for_company_name():
    res = resolve_symbol("Nvidia")
    assert res["symbol"] == "NVDA"
    assert res["source"] == "alias"


def test_resolver_uses_yahoo_for_long_uppercase_company_name():
    old_aliases = resolver.COMMON_ALIASES
    resolver.COMMON_ALIASES = {}
    old_sec = resolver.sec_company_search
    resolver.sec_company_search = lambda query, limit=8, timeout=6.0: []
    old = resolver.yahoo_search
    resolver.yahoo_search = lambda query, limit=8, timeout=6.0: [{
        "symbol": "NVDA",
        "name": "NVIDIA Corporation",
        "exchange": "NMS",
        "type": "EQUITY",
    }]
    try:
        res = resolve_symbol("NVIDIA")
        assert res["symbol"] == "NVDA"
        assert res["source"] == "yahoo"
    finally:
        resolver.yahoo_search = old
        resolver.sec_company_search = old_sec
        resolver.COMMON_ALIASES = old_aliases


def test_resolver_uses_sec_company_tickers_before_yahoo():
    old_aliases = resolver.COMMON_ALIASES
    old_sec = resolver.sec_company_search
    old_yahoo = resolver.yahoo_search
    resolver.COMMON_ALIASES = {}
    resolver.sec_company_search = lambda query, limit=8, timeout=6.0: [{
        "symbol": "SNOW",
        "name": "Snowflake Inc.",
        "exchange": None,
        "type": "EQUITY",
        "score": 0.97,
    }]
    resolver.yahoo_search = lambda query, limit=8, timeout=6.0: [{
        "symbol": "WRONG",
        "name": "Wrong Result Inc.",
        "type": "EQUITY",
    }]
    try:
        res = resolve_symbol("Snowflake")
        assert res["symbol"] == "SNOW"
        assert res["name"] == "Snowflake Inc."
        assert res["source"] == "sec"
    finally:
        resolver.yahoo_search = old_yahoo
        resolver.sec_company_search = old_sec
        resolver.COMMON_ALIASES = old_aliases


def test_resolver_uses_sec_for_company_name_option_request():
    old_aliases = resolver.COMMON_ALIASES
    old_sec = resolver.sec_company_search
    old_yahoo = resolver.yahoo_search
    resolver.COMMON_ALIASES = {}
    resolver.sec_company_search = lambda query, limit=8, timeout=6.0: [{
        "symbol": "SNOW",
        "name": "Snowflake Inc.",
        "type": "EQUITY",
        "score": 0.97,
    }]
    resolver.yahoo_search = lambda query, limit=8, timeout=6.0: []
    try:
        res = resolve_symbol("Snowflake 20260618 C 200")
        assert res["symbol"] == "SNOW"
        assert res["source"] == "sec"
        assert res["request"]["ticker"] == "SNOW"
        assert res["request"]["ticker_source"] == "sec"
        assert res["request"]["expiry"] == "2026-06-18"
        assert res["request"]["side"] == "call"
        assert res["request"]["strike"] == 200.0
    finally:
        resolver.yahoo_search = old_yahoo
        resolver.sec_company_search = old_sec
        resolver.COMMON_ALIASES = old_aliases


def test_sec_company_search_scores_cached_rows():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "sec_company_tickers.json"
        cache.write_text(json.dumps({
            "rows": [
                {"symbol": "SNOW", "name": "Snowflake Inc.", "cik": 1640147},
                {"symbol": "SNAP", "name": "Snap Inc.", "cik": 1564408},
            ],
        }), encoding="utf-8")
        old_cache = resolver.SEC_TICKER_CACHE
        old_fetch = resolver.fetch_sec_company_tickers
        resolver.SEC_TICKER_CACHE = cache
        resolver.fetch_sec_company_tickers = lambda timeout=6.0: []
        try:
            matches = resolver.sec_company_search("Snowflake", limit=3)
            assert matches[0]["symbol"] == "SNOW"
            assert matches[0]["source"] == "sec_company_tickers"
        finally:
            resolver.fetch_sec_company_tickers = old_fetch
            resolver.SEC_TICKER_CACHE = old_cache


if __name__ == "__main__":
    test_resolver_accepts_direct_ticker()
    test_resolver_extracts_underlying_from_option_text()
    test_resolver_reports_empty_query()
    test_resolver_uses_yahoo_for_company_name()
    test_resolver_uses_yahoo_for_long_uppercase_company_name()
    test_resolver_uses_sec_company_tickers_before_yahoo()
    test_resolver_uses_sec_for_company_name_option_request()
    test_sec_company_search_scores_cached_rows()
    print("8/8 symbol resolver tests passed")

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
    resolver.sec_company_search = lambda query, limit=8, timeout=6.0, fetch_if_stale=True: []
    old_nasdaq = resolver.nasdaq_symbol_search
    resolver.nasdaq_symbol_search = lambda query, limit=8, timeout=8.0, fetch_if_stale=True: []
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
        resolver.nasdaq_symbol_search = old_nasdaq
        resolver.sec_company_search = old_sec
        resolver.COMMON_ALIASES = old_aliases


def test_resolver_uses_sec_company_tickers_before_yahoo():
    old_aliases = resolver.COMMON_ALIASES
    old_sec = resolver.sec_company_search
    old_yahoo = resolver.yahoo_search
    resolver.COMMON_ALIASES = {}
    resolver.sec_company_search = lambda query, limit=8, timeout=6.0, fetch_if_stale=True: [{
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


def test_resolver_uses_nasdaq_directory_before_yahoo():
    old_aliases = resolver.COMMON_ALIASES
    old_sec = resolver.sec_company_search
    old_nasdaq = resolver.nasdaq_symbol_search
    old_yahoo = resolver.yahoo_search
    resolver.COMMON_ALIASES = {}
    resolver.sec_company_search = lambda query, limit=8, timeout=6.0, fetch_if_stale=True: []
    resolver.nasdaq_symbol_search = lambda query, limit=8, timeout=8.0, fetch_if_stale=True: [{
        "symbol": "QQQ",
        "name": "Invesco QQQ Trust",
        "exchange": "NASDAQ Global Market",
        "type": "ETF",
        "score": 0.94,
    }]
    resolver.yahoo_search = lambda query, limit=8, timeout=6.0: [{
        "symbol": "WRONG",
        "name": "Wrong Result Inc.",
        "type": "EQUITY",
    }]
    try:
        res = resolve_symbol("Invesco QQQ Trust")
        assert res["symbol"] == "QQQ"
        assert res["name"] == "Invesco QQQ Trust"
        assert res["source"] == "nasdaq"
    finally:
        resolver.yahoo_search = old_yahoo
        resolver.nasdaq_symbol_search = old_nasdaq
        resolver.sec_company_search = old_sec
        resolver.COMMON_ALIASES = old_aliases


def test_resolver_uses_sec_for_company_name_option_request():
    old_aliases = resolver.COMMON_ALIASES
    old_sec = resolver.sec_company_search
    old_yahoo = resolver.yahoo_search
    resolver.COMMON_ALIASES = {}
    resolver.sec_company_search = lambda query, limit=8, timeout=6.0, fetch_if_stale=True: [{
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


def test_resolver_uses_nasdaq_for_company_name_option_request():
    old_aliases = resolver.COMMON_ALIASES
    old_sec = resolver.sec_company_search
    old_nasdaq = resolver.nasdaq_symbol_search
    old_yahoo = resolver.yahoo_search
    resolver.COMMON_ALIASES = {}
    resolver.sec_company_search = lambda query, limit=8, timeout=6.0, fetch_if_stale=True: []
    resolver.nasdaq_symbol_search = lambda query, limit=8, timeout=8.0, fetch_if_stale=True: [{
        "symbol": "QQQ",
        "name": "Invesco QQQ Trust",
        "type": "ETF",
        "score": 0.94,
    }]
    resolver.yahoo_search = lambda query, limit=8, timeout=6.0: []
    try:
        res = resolve_symbol("Invesco QQQ Trust 20261218 C 500")
        assert res["symbol"] == "QQQ"
        assert res["source"] == "nasdaq"
        assert res["request"]["ticker"] == "QQQ"
        assert res["request"]["ticker_source"] == "nasdaq"
        assert res["request"]["expiry"] == "2026-12-18"
        assert res["request"]["side"] == "call"
        assert res["request"]["strike"] == 500.0
    finally:
        resolver.yahoo_search = old_yahoo
        resolver.nasdaq_symbol_search = old_nasdaq
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


def test_sec_company_cache_meta_reports_missing_and_rows():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "missing.json"
        missing = resolver.sec_company_cache_meta(cache)
        assert missing["status"] == "missing"
        assert missing["exists"] is False

        cache.write_text(json.dumps({
            "rows": [
                {"symbol": "SNOW", "name": "Snowflake Inc."},
                {"symbol": "AAPL", "name": "Apple Inc."},
            ],
        }), encoding="utf-8")
        meta = resolver.sec_company_cache_meta(cache)
        assert meta["status"] == "fresh"
        assert meta["exists"] is True
        assert meta["row_count"] == 2


def test_nasdaq_symbol_directory_parses_and_scores_cached_rows():
    listed_text = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
NVDA|NVIDIA Corporation - Common Stock|Q|N|N|100|N|N
TEST|Test Company - Common Stock|G|Y|N|100|N|N
File Creation Time:0614202600:00|||||||
"""
    other_text = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
QQQ|Invesco QQQ Trust, Series 1|P|QQQ|Y|100|N|QQQ
"""
    rows = resolver._parse_nasdaq_symbol_text(listed_text, "nasdaq")
    rows += resolver._parse_nasdaq_symbol_text(other_text, "other")
    assert {row["symbol"] for row in rows} == {"NVDA", "TEST", "QQQ"}
    assert [row for row in rows if row["symbol"] == "QQQ"][0]["type"] == "ETF"

    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "nasdaq_symbol_directory.json"
        cache.write_text(json.dumps({"rows": rows}), encoding="utf-8")
        old_cache = resolver.NASDAQ_SYMBOL_CACHE
        old_fetch = resolver.fetch_nasdaq_symbol_directory
        resolver.NASDAQ_SYMBOL_CACHE = cache
        resolver.fetch_nasdaq_symbol_directory = lambda timeout=8.0: []
        try:
            matches = resolver.nasdaq_symbol_search("NVIDIA", limit=3)
            assert matches[0]["symbol"] == "NVDA"
            assert matches[0]["source"] == "nasdaq_symbol_directory"
            assert all(row["symbol"] != "TEST" for row in matches)
            meta = resolver.nasdaq_symbol_cache_meta(cache)
            assert meta["status"] == "fresh"
            assert meta["row_count"] == 3
        finally:
            resolver.fetch_nasdaq_symbol_directory = old_fetch
            resolver.NASDAQ_SYMBOL_CACHE = old_cache


if __name__ == "__main__":
    test_resolver_accepts_direct_ticker()
    test_resolver_extracts_underlying_from_option_text()
    test_resolver_reports_empty_query()
    test_resolver_uses_yahoo_for_company_name()
    test_resolver_uses_yahoo_for_long_uppercase_company_name()
    test_resolver_uses_sec_company_tickers_before_yahoo()
    test_resolver_uses_nasdaq_directory_before_yahoo()
    test_resolver_uses_sec_for_company_name_option_request()
    test_resolver_uses_nasdaq_for_company_name_option_request()
    test_sec_company_search_scores_cached_rows()
    test_sec_company_cache_meta_reports_missing_and_rows()
    test_nasdaq_symbol_directory_parses_and_scores_cached_rows()
    print("12/12 symbol resolver tests passed")

import sys
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
    }


def test_resolver_reports_empty_query():
    res = resolve_symbol("")
    assert res["symbol"] is None
    assert res["error"] == "empty query"


def test_resolver_uses_yahoo_for_company_name():
    old = resolver.yahoo_search
    resolver.yahoo_search = lambda query, limit=8, timeout=6.0: [{
        "symbol": "NVDA",
        "name": "NVIDIA Corporation",
        "exchange": "NMS",
        "type": "EQUITY",
    }]
    try:
        res = resolve_symbol("Nvidia")
        assert res["symbol"] == "NVDA"
        assert res["source"] == "yahoo"
    finally:
        resolver.yahoo_search = old


def test_resolver_uses_yahoo_for_long_uppercase_company_name():
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


if __name__ == "__main__":
    test_resolver_accepts_direct_ticker()
    test_resolver_extracts_underlying_from_option_text()
    test_resolver_reports_empty_query()
    test_resolver_uses_yahoo_for_company_name()
    test_resolver_uses_yahoo_for_long_uppercase_company_name()
    print("5/5 symbol resolver tests passed")

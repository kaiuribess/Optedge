# Purpose: Test free historical price-provider fallbacks.
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.text.encode("utf-8")


def test_stooq_symbol_mapping():
    assert data_provider._stooq_symbol("AAPL") == "aapl.us"
    assert data_provider._stooq_symbol("BRK-B") == "brk.b.us"
    assert data_provider._stooq_symbol("^GSPC") == "^spx"
    assert data_provider._stooq_symbol("CL=F") == "cl.f"
    assert data_provider._stooq_symbol("EURUSD=X") is None


def test_stooq_history_parses_public_csv_without_network(monkeypatch=None):
    import urllib.request

    original = urllib.request.urlopen
    seen_urls: list[str] = []

    def fake_urlopen(req, timeout=0):
        seen_urls.append(req.full_url)
        return _FakeResponse(
            "Date,Open,High,Low,Close,Volume\n"
            "2026-06-10,10,11,9,10.5,1000\n"
            "2026-06-11,11,13,10,12.5,1500\n"
        )

    try:
        urllib.request.urlopen = fake_urlopen
        hist = data_provider._stooq_history("AAPL", period="max", interval="1d")
    finally:
        urllib.request.urlopen = original

    assert not hist.empty
    assert list(hist.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert float(hist["Close"].iloc[-1]) == 12.5
    assert hist.attrs["history_source"] == "stooq_csv"
    assert hist.attrs["history_quality"] == "delayed"
    assert "s=aapl.us" in seen_urls[0]
    assert "i=d" in seen_urls[0]


def test_nasdaq_history_parses_public_json_without_network():
    import urllib.request

    original = urllib.request.urlopen
    seen_urls: list[str] = []

    def fake_urlopen(req, timeout=0):
        seen_urls.append(req.full_url)
        return _FakeResponse(
            '{"data":{"symbol":"AAPL","totalRecords":2,"tradesTable":{"rows":['
            '{"date":"06/11/2026","close":"$12.50","volume":"1,500","open":"$11.00","high":"$13.00","low":"$10.00"},'
            '{"date":"06/10/2026","close":"$10.50","volume":"1,000","open":"$10.00","high":"$11.00","low":"$9.00"}'
            ']}}}'
        )

    try:
        urllib.request.urlopen = fake_urlopen
        hist = data_provider._nasdaq_history("AAPL", period="1mo", interval="1d")
    finally:
        urllib.request.urlopen = original

    assert not hist.empty
    assert list(hist.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert float(hist["Close"].iloc[-1]) == 12.5
    assert hist.attrs["history_source"] == "nasdaq_historical"
    assert hist.attrs["price_basis"] == "unadjusted_close"
    assert hist.attrs["history_quality"] == "free_or_delayed"
    assert "assetclass=stocks" in seen_urls[0]
    assert "/quote/AAPL/historical" in seen_urls[0]


def test_get_history_uses_nasdaq_after_yahoo_failures():
    old_cache_get = data_provider.cache_get
    old_cache_put = data_provider.cache_put
    old_yahoo = data_provider._yahoo_v8_history
    old_yf_ticker = data_provider.yf_ticker
    old_nasdaq = data_provider._nasdaq_history
    old_stooq = data_provider._stooq_history
    stored = {}

    idx = pd.to_datetime(["2026-06-10", "2026-06-11"], utc=True)
    stooq_df = pd.DataFrame({
        "Open": [10.0, 11.0],
        "High": [11.0, 13.0],
        "Low": [9.0, 10.0],
        "Close": [10.5, 12.5],
        "Volume": [1000, 1500],
    }, index=idx)

    class EmptyTicker:
        def history(self, period="1y", interval="1d"):
            return pd.DataFrame()

    try:
        data_provider.cache_get = lambda *args, **kwargs: None
        data_provider.cache_put = lambda key, value: stored.update({key: value})
        data_provider._yahoo_v8_history = lambda *args, **kwargs: pd.DataFrame()
        data_provider.yf_ticker = lambda ticker: EmptyTicker()
        data_provider._nasdaq_history = lambda ticker, period, interval: stooq_df
        data_provider._stooq_history = lambda ticker, period, interval: (_ for _ in ()).throw(
            AssertionError("Stooq should not be called when Nasdaq succeeds")
        )

        hist = data_provider.get_history("AAPL", period="1mo", interval="1d")
    finally:
        data_provider.cache_get = old_cache_get
        data_provider.cache_put = old_cache_put
        data_provider._yahoo_v8_history = old_yahoo
        data_provider.yf_ticker = old_yf_ticker
        data_provider._nasdaq_history = old_nasdaq
        data_provider._stooq_history = old_stooq

    assert not hist.empty
    assert float(hist["Close"].iloc[-1]) == 12.5
    assert hist.attrs["history_source"] == "nasdaq_historical"
    assert "history:AAPL:1mo:1d" in stored
    assert stored["history:AAPL:1mo:1d"][0]["_history_source"] == "nasdaq_historical"
    assert stored["history:AAPL:1mo:1d"][0]["_history_price_basis"] == "unadjusted_close"


def test_get_history_uses_stooq_after_other_free_sources_fail():
    old_cache_get = data_provider.cache_get
    old_cache_put = data_provider.cache_put
    old_yahoo = data_provider._yahoo_v8_history
    old_yf_ticker = data_provider.yf_ticker
    old_nasdaq = data_provider._nasdaq_history
    old_stooq = data_provider._stooq_history
    stored = {}

    idx = pd.to_datetime(["2026-06-10", "2026-06-11"], utc=True)
    stooq_df = pd.DataFrame({
        "Open": [10.0, 11.0],
        "High": [11.0, 13.0],
        "Low": [9.0, 10.0],
        "Close": [10.5, 12.5],
        "Volume": [1000, 1500],
    }, index=idx)

    class EmptyTicker:
        def history(self, period="1y", interval="1d"):
            return pd.DataFrame()

    try:
        data_provider.cache_get = lambda *args, **kwargs: None
        data_provider.cache_put = lambda key, value: stored.update({key: value})
        data_provider._yahoo_v8_history = lambda *args, **kwargs: pd.DataFrame()
        data_provider.yf_ticker = lambda ticker: EmptyTicker()
        data_provider._nasdaq_history = lambda *args, **kwargs: pd.DataFrame()
        data_provider._stooq_history = lambda ticker, period, interval: stooq_df

        hist = data_provider.get_history("AAPL", period="1mo", interval="1d")
    finally:
        data_provider.cache_get = old_cache_get
        data_provider.cache_put = old_cache_put
        data_provider._yahoo_v8_history = old_yahoo
        data_provider.yf_ticker = old_yf_ticker
        data_provider._nasdaq_history = old_nasdaq
        data_provider._stooq_history = old_stooq

    assert not hist.empty
    assert float(hist["Close"].iloc[-1]) == 12.5
    assert hist.attrs["history_source"] == "stooq_csv"
    assert hist.attrs["history_quality"] == "delayed"
    assert hist.attrs["price_basis"] == "unknown"
    assert stored["history:AAPL:1mo:1d"][0]["_history_source"] == "stooq_csv"
    cached = data_provider._history_from_cache(stored["history:AAPL:1mo:1d"])
    assert cached.attrs["history_source"] == "stooq_csv"
    assert cached.attrs["price_basis"] == "unknown"


if __name__ == "__main__":
    test_stooq_symbol_mapping()
    test_stooq_history_parses_public_csv_without_network()
    test_nasdaq_history_parses_public_json_without_network()
    test_get_history_uses_nasdaq_after_yahoo_failures()
    test_get_history_uses_stooq_after_other_free_sources_fail()
    print("5/5 public data provider tests passed")

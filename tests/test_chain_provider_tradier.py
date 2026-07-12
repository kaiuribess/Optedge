# Purpose: Test option chains across free and keyed providers.
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chain_provider
import data_provider


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers, timeout))
        if url.endswith("/markets/options/expirations"):
            return _Resp({"expirations": {"date": ["2026-06-18", "2026-07-17"]}})
        if url.endswith("/markets/options/chains"):
            exp = params["expiration"]
            return _Resp({
                "options": {
                    "option": [
                        {
                            "symbol": f"AAPL{exp[2:4]}{exp[5:7]}{exp[8:10]}C00280000",
                            "option_type": "call",
                            "strike": 280,
                            "bid": 1.1,
                            "ask": 1.3,
                            "last": None,
                            "volume": 10,
                            "open_interest": 200,
                            "underlying_price": 300.0,
                            "greeks": {"delta": 0.42, "gamma": 0.01, "theta": -0.02, "vega": 0.1, "mid_iv": 0.35},
                        },
                        {
                            "symbol": f"AAPL{exp[2:4]}{exp[5:7]}{exp[8:10]}P00280000",
                            "option_type": "put",
                            "strike": 280,
                            "bid": 0.9,
                            "ask": 1.0,
                            "last": 0.95,
                            "volume": 8,
                            "open_interest": 150,
                            "underlying_price": 300.0,
                            "greeks": {"delta": -0.28, "smv_vol": 0.33},
                        },
                    ]
                }
            })
        raise AssertionError(f"unexpected url {url}")


class _YahooSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers, timeout))
        exp = 1781740800 if not params else int(params["date"])
        return _Resp({
            "optionChain": {
                "result": [{
                    "quote": {
                        "regularMarketPrice": 300.0,
                        "trailingAnnualDividendYield": 0.005,
                    },
                    "expirationDates": [1781740800, 1784332800],
                    "options": [{
                        "expirationDate": exp,
                        "calls": [{
                            "contractSymbol": "AAPL260618C00280000",
                            "strike": 280,
                            "bid": 1.1,
                            "ask": 1.3,
                            "lastPrice": None,
                            "volume": 10,
                            "openInterest": 200,
                            "impliedVolatility": 0.35,
                            "inTheMoney": True,
                            "lastTradeDate": 1779210000,
                        }],
                        "puts": [{
                            "contractSymbol": "AAPL260618P00280000",
                            "strike": 280,
                            "bid": 0.9,
                            "ask": 1.0,
                            "lastPrice": 0.95,
                            "volume": 8,
                            "openInterest": 150,
                            "impliedVolatility": 0.33,
                            "inTheMoney": False,
                        }],
                    }],
                }],
                "error": None,
            }
        })


class _FailingYahooSession:
    def get(self, url, params=None, headers=None, timeout=None):
        return _Resp({}, status_code=401)


class _FakeYfTicker:
    def __init__(self):
        self._expirations = {
            "2026-06-18": 1781740800,
            "2026-07-18": 1784332800,
        }

    def _download_options(self, date=None):
        exp = 1781740800 if date is None else int(date)
        strike = 280 if exp == 1781740800 else 290
        return {
            "underlying": {
                "regularMarketPrice": 300.0,
                "trailingAnnualDividendYield": 0.005,
            },
            "expirationDate": exp,
            "calls": [{
                "contractSymbol": f"AAPL260618C00{strike}000",
                "strike": strike,
                "bid": 1.1,
                "ask": 1.3,
                "lastPrice": None,
                "volume": 10,
                "openInterest": 200,
                "impliedVolatility": 0.35,
            }],
            "puts": [],
        }


def test_tradier_disabled_without_token():
    old = os.environ.pop("OPTEDGE_TRADIER_TOKEN", None)
    try:
        assert chain_provider.tradier_enabled() is False
        assert chain_provider._fetch_tradier("AAPL", _Session()) is None
    finally:
        if old is not None:
            os.environ["OPTEDGE_TRADIER_TOKEN"] = old


def test_tradier_fetch_normalizes_chain():
    old = os.environ.get("OPTEDGE_TRADIER_TOKEN")
    os.environ["OPTEDGE_TRADIER_TOKEN"] = "test-token"
    try:
        blob = chain_provider._fetch_tradier("AAPL", _Session())
    finally:
        if old is None:
            os.environ.pop("OPTEDGE_TRADIER_TOKEN", None)
        else:
            os.environ["OPTEDGE_TRADIER_TOKEN"] = old

    assert blob["source"] == "tradier"
    assert blob["spot"] == 300.0
    assert blob["quote_quality"] == "live_or_broker"
    assert blob["expirations"] == ["2026-06-18", "2026-07-17"]
    first = blob["chains"]["2026-06-18"]
    assert set(first["side"]) == {"call", "put"}
    call = first[first["side"] == "call"].iloc[0]
    assert round(call["lastPrice"], 2) == 1.2
    assert call["openInterest"] == 200
    assert call["impliedVolatility"] == 0.35
    assert call["delta"] == 0.42


def test_yahoo_options_fetch_normalizes_direct_chain():
    session = _YahooSession()
    blob = chain_provider._fetch_yahoo_options("AAPL", session)

    assert blob["source"] == "yahoo_options"
    assert blob["spot"] == 300.0
    assert blob["div_yield"] == 0.005
    assert blob["quote_quality"] == "free_or_delayed"
    assert blob["expirations"] == ["2026-06-18", "2026-07-18"]
    assert len(session.calls) == 2
    first = blob["chains"]["2026-06-18"]
    assert set(first["side"]) == {"call", "put"}
    call = first[first["side"] == "call"].iloc[0]
    assert round(call["lastPrice"], 2) == 1.2
    assert call["openInterest"] == 200
    assert call["impliedVolatility"] == 0.35
    assert call["contractSymbol"] == "AAPL260618C00280000"


def test_yahoo_options_falls_back_to_bounded_yfinance_downloader():
    old_yf_ticker = data_provider.yf_ticker
    data_provider.yf_ticker = lambda ticker: _FakeYfTicker()
    try:
        blob = chain_provider._fetch_yahoo_options("AAPL", _FailingYahooSession())
    finally:
        data_provider.yf_ticker = old_yf_ticker

    assert blob["source"] == "yahoo_options"
    assert blob["expirations"] == ["2026-06-18", "2026-07-18"]
    assert len(blob["chains"]["2026-06-18"]) == 1
    assert len(blob["chains"]["2026-07-18"]) == 1


def test_fetch_chain_records_free_provider_diagnostics():
    old_env = {
        key: os.environ.get(key)
        for key in ("OPTEDGE_TRADIER_TOKEN", "TRADIER_TOKEN", "TRADIER_ACCESS_TOKEN")
    }
    for key in old_env:
        os.environ.pop(key, None)
    old_cache_get = data_provider.cache_get
    old_cache_put = data_provider.cache_put
    old_get_session = data_provider.get_session
    old_cboe = chain_provider._fetch_cboe
    old_nasdaq = chain_provider._fetch_nasdaq
    old_yahoo = chain_provider._fetch_yahoo_options
    old_yfinance = chain_provider._fetch_yfinance
    cached = {}

    def fake_nasdaq(ticker, session, asset_class="stocks"):
        if asset_class != "etf":
            return None
        return {
            "spot": 450.0,
            "div_yield": 0.0,
            "expirations": ["2026-09-18"],
            "chains": {"2026-09-18": pd.DataFrame([{
                "strike": 450,
                "side": "call",
                "bid": 5.0,
                "ask": 5.2,
                "lastPrice": 5.1,
                "volume": 10,
                "openInterest": 200,
            }])},
            "source": f"nasdaq_{asset_class}",
            "quote_quality": "free_or_delayed",
            "data_delay": "delayed",
        }

    data_provider.cache_get = lambda *args, **kwargs: None
    data_provider.cache_put = lambda key, blob: cached.update({key: blob})
    data_provider.get_session = lambda: object()
    chain_provider._fetch_cboe = lambda *args, **kwargs: None
    chain_provider._fetch_nasdaq = fake_nasdaq
    chain_provider._fetch_yahoo_options = lambda *args, **kwargs: None
    chain_provider._fetch_yfinance = lambda *args, **kwargs: None
    try:
        blob = chain_provider.fetch_chain("SPY", cache_age=0, include_diagnostics=True)
        first_receipt = blob["provider_response_received_at"]
        data_provider.cache_get = lambda key, *args, **kwargs: cached.get(key)
        cached_blob = chain_provider.fetch_chain("SPY", cache_age=600, include_diagnostics=True)
    finally:
        data_provider.cache_get = old_cache_get
        data_provider.cache_put = old_cache_put
        data_provider.get_session = old_get_session
        chain_provider._fetch_cboe = old_cboe
        chain_provider._fetch_nasdaq = old_nasdaq
        chain_provider._fetch_yahoo_options = old_yahoo
        chain_provider._fetch_yfinance = old_yfinance
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert blob["source"] == "nasdaq_etf"
    assert blob["quote_quality"] == "free_or_delayed"
    assert blob["data_delay"] == "delayed"
    assert blob["source_quote_at"] == first_receipt
    assert blob["source_quote_time_basis"] == "provider_response_received_at"
    assert cached_blob["provider_response_received_at"] == first_receipt
    assert cached_blob["source_quote_at"] == first_receipt
    assert [row["provider"] for row in blob["source_attempts"]] == [
        "cboe",
        "nasdaq_stocks",
        "nasdaq_etf",
    ]
    assert blob["source_attempts"][0]["status"] == "warn"
    assert blob["source_attempts"][-1]["status"] == "ok"
    assert "chain:SPY" in cached
    assert cached["chain:SPY"]["source_attempts"][-1]["provider"] == "nasdaq_etf"


def test_fetch_chain_uses_yahoo_options_before_yfinance():
    old_env = {
        key: os.environ.get(key)
        for key in ("OPTEDGE_TRADIER_TOKEN", "TRADIER_TOKEN", "TRADIER_ACCESS_TOKEN")
    }
    for key in old_env:
        os.environ.pop(key, None)
    old_cache_get = data_provider.cache_get
    old_cache_put = data_provider.cache_put
    old_get_session = data_provider.get_session
    old_cboe = chain_provider._fetch_cboe
    old_nasdaq = chain_provider._fetch_nasdaq
    old_yahoo = chain_provider._fetch_yahoo_options
    old_yfinance = chain_provider._fetch_yfinance
    yfinance_called = {"value": False}

    yahoo_blob = {
        "spot": 300.0,
        "div_yield": 0.0,
        "expirations": ["2026-06-18"],
        "chains": {"2026-06-18": pd.DataFrame([{
            "strike": 280,
            "side": "call",
            "bid": 1.0,
            "ask": 1.2,
            "lastPrice": 1.1,
            "volume": 5,
            "openInterest": 100,
        }])},
        "source": "yahoo_options",
        "quote_quality": "free_or_delayed",
        "data_delay": "delayed_or_research",
    }

    def fake_yfinance(*args, **kwargs):
        yfinance_called["value"] = True
        return None

    data_provider.cache_get = lambda *args, **kwargs: None
    data_provider.cache_put = lambda *args, **kwargs: None
    data_provider.get_session = lambda: object()
    chain_provider._fetch_cboe = lambda *args, **kwargs: None
    chain_provider._fetch_nasdaq = lambda *args, **kwargs: None
    chain_provider._fetch_yahoo_options = lambda *args, **kwargs: yahoo_blob
    chain_provider._fetch_yfinance = fake_yfinance
    try:
        blob = chain_provider.fetch_chain("AAPL", cache_age=0, include_diagnostics=True)
    finally:
        data_provider.cache_get = old_cache_get
        data_provider.cache_put = old_cache_put
        data_provider.get_session = old_get_session
        chain_provider._fetch_cboe = old_cboe
        chain_provider._fetch_nasdaq = old_nasdaq
        chain_provider._fetch_yahoo_options = old_yahoo
        chain_provider._fetch_yfinance = old_yfinance
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert blob["source"] == "yahoo_options"
    assert yfinance_called["value"] is False
    assert [row["provider"] for row in blob["source_attempts"]] == [
        "cboe",
        "nasdaq_stocks",
        "nasdaq_etf",
        "nasdaq_index",
        "yahoo_options",
    ]


if __name__ == "__main__":
    test_tradier_disabled_without_token()
    test_tradier_fetch_normalizes_chain()
    test_yahoo_options_fetch_normalizes_direct_chain()
    test_yahoo_options_falls_back_to_bounded_yfinance_downloader()
    test_fetch_chain_records_free_provider_diagnostics()
    test_fetch_chain_uses_yahoo_options_before_yfinance()
    print("6/6 Tradier chain provider tests passed")

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
    chain_provider._fetch_yfinance = lambda *args, **kwargs: None
    try:
        blob = chain_provider.fetch_chain("SPY", cache_age=0, include_diagnostics=True)
    finally:
        data_provider.cache_get = old_cache_get
        data_provider.cache_put = old_cache_put
        data_provider.get_session = old_get_session
        chain_provider._fetch_cboe = old_cboe
        chain_provider._fetch_nasdaq = old_nasdaq
        chain_provider._fetch_yfinance = old_yfinance
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert blob["source"] == "nasdaq_etf"
    assert blob["quote_quality"] == "free_or_delayed"
    assert blob["data_delay"] == "delayed"
    assert [row["provider"] for row in blob["source_attempts"]] == [
        "cboe",
        "nasdaq_stocks",
        "nasdaq_etf",
    ]
    assert blob["source_attempts"][0]["status"] == "warn"
    assert blob["source_attempts"][-1]["status"] == "ok"
    assert "chain:SPY" in cached
    assert cached["chain:SPY"]["source_attempts"][-1]["provider"] == "nasdaq_etf"


if __name__ == "__main__":
    test_tradier_disabled_without_token()
    test_tradier_fetch_normalizes_chain()
    test_fetch_chain_records_free_provider_diagnostics()
    print("3/3 Tradier chain provider tests passed")

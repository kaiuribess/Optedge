import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chain_provider


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


if __name__ == "__main__":
    test_tradier_disabled_without_token()
    test_tradier_fetch_normalizes_chain()
    print("2/2 Tradier chain provider tests passed")

# Purpose: Test public Nasdaq small-cap mover ranking.
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402
from engines import nasdaq_screener  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _payload():
    return {
        "data": {
            "headers": {"symbol": "Symbol"},
            "rows": [
                {
                    "symbol": "MOVE",
                    "name": "Move Corp",
                    "lastsale": "$4.25",
                    "netchange": "0.33",
                    "pctchange": "8.4%",
                    "volume": "2,500,000",
                    "marketCap": "220000000",
                    "country": "United States",
                    "sector": "Technology",
                    "industry": "Software",
                    "url": "/market-activity/stocks/move",
                },
                {
                    "symbol": "BIG",
                    "name": "Big Corp",
                    "lastsale": "$120.00",
                    "netchange": "1.00",
                    "pctchange": "1.0%",
                    "volume": "100000",
                    "marketCap": "100000000000",
                    "country": "United States",
                    "sector": "Technology",
                    "industry": "Software",
                    "url": "/market-activity/stocks/big",
                },
                {
                    "symbol": "DROP",
                    "name": "Drop Corp",
                    "lastsale": "$2.50",
                    "netchange": "-0.40",
                    "pctchange": "-12.5%",
                    "volume": "900000",
                    "marketCap": "75000000",
                    "country": "United States",
                    "sector": "Healthcare",
                    "industry": "Biotechnology",
                    "url": "/market-activity/stocks/drop",
                },
            ],
        }
    }


def test_fetch_stock_screener_parses_public_rows():
    old_cache_get = data_provider.cache_get
    old_cache_put = data_provider.cache_put
    old_urlopen = nasdaq_screener.urllib.request.urlopen
    cached = {}

    data_provider.cache_get = lambda *args, **kwargs: None
    data_provider.cache_put = lambda key, value: cached.update({key: value})
    nasdaq_screener.urllib.request.urlopen = lambda request, timeout=18: _Resp(_payload())
    try:
        df = nasdaq_screener.fetch_stock_screener(cache_age=0)
    finally:
        data_provider.cache_get = old_cache_get
        data_provider.cache_put = old_cache_put
        nasdaq_screener.urllib.request.urlopen = old_urlopen

    assert len(df) == 3
    move = df[df["symbol"] == "MOVE"].iloc[0]
    assert move["last_price"] == 4.25
    assert move["pct_change"] == 8.4
    assert move["volume"] == 2_500_000
    assert move["market_cap"] == 220_000_000
    assert move["source"] == "nasdaq_screener"
    assert "nasdaq_screener:stocks:download" in cached


def test_small_cap_movers_filters_and_scores():
    old_fetch = nasdaq_screener.fetch_stock_screener
    try:
        nasdaq_screener.fetch_stock_screener = lambda cache_age=1800: old_fetch(cache_age=0)
        old_cache_get = data_provider.cache_get
        old_cache_put = data_provider.cache_put
        old_urlopen = nasdaq_screener.urllib.request.urlopen
        data_provider.cache_get = lambda *args, **kwargs: None
        data_provider.cache_put = lambda *args, **kwargs: None
        nasdaq_screener.urllib.request.urlopen = lambda request, timeout=18: _Resp(_payload())
        try:
            movers = nasdaq_screener.small_cap_movers(max_rows=10, cache_age=0)
        finally:
            data_provider.cache_get = old_cache_get
            data_provider.cache_put = old_cache_put
            nasdaq_screener.urllib.request.urlopen = old_urlopen
    finally:
        nasdaq_screener.fetch_stock_screener = old_fetch

    assert set(movers["symbol"]) == {"MOVE", "DROP"}
    assert "BIG" not in set(movers["symbol"])
    assert all(movers["nasdaq_mover_score"] >= 70)
    assert set(movers["mover_direction"]) == {"up", "down"}
    assert set(movers["market_cap_bucket"]) == {"micro"}


if __name__ == "__main__":
    test_fetch_stock_screener_parses_public_rows()
    test_small_cap_movers_filters_and_scores()
    print("2/2 Nasdaq screener tests passed")

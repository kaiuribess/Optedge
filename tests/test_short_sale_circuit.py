# Purpose: Test short-sale circuit-breaker notices.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import short_sale_circuit


SAMPLE_FILE = """Symbol,Security Name,Market Category,Trigger Time
MOVE,Move Corp Cmn,R,6/16/2026 9:30:00 AM
DROP,"Drop Holdings, Inc. Cl A",Q,6/16/2026 10:15:30 AM
20260616041500
"""


class _Response:
    def __init__(self, text: str):
        self.status_code = 200
        self.text = text


class _Session:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout=12):
        self.urls.append((url, timeout))
        if url == short_sale_circuit.SHORT_SALE_CIRCUIT_PAGE_URL:
            return _Response(
                '<a href="/dynamic/symdir/shorthalts/shorthalts20260616.txt">Download</a>'
            )
        assert url == "https://www.nasdaqtrader.com/dynamic/symdir/shorthalts/shorthalts20260616.txt"
        return _Response(SAMPLE_FILE)


def test_parse_short_sale_circuit_file_handles_csv_quotes_and_timestamp():
    df = short_sale_circuit.parse_short_sale_circuit_file(
        SAMPLE_FILE,
        source_url="https://www.nasdaqtrader.com/dynamic/symdir/shorthalts/shorthalts20260616.txt",
    )

    assert list(df["symbol"]) == ["DROP", "MOVE"]
    drop = df[df["symbol"] == "DROP"].iloc[0]
    move = df[df["symbol"] == "MOVE"].iloc[0]
    assert drop["name"] == "Drop Holdings, Inc. Cl A"
    assert drop["market_category"] == "Q"
    assert drop["triggered_at"].endswith("-04:00")
    assert bool(drop["short_sale_restricted"]) is True
    assert move["ssr_risk_score"] == 82
    assert move["file_timestamp"] == "20260616041500"
    assert move["source"] == short_sale_circuit.SOURCE_NAME


def test_fetch_short_sale_circuit_breakers_uses_download_link_and_cache():
    old_cache_get = short_sale_circuit.data_provider.cache_get
    old_cache_put = short_sale_circuit.data_provider.cache_put
    old_get_session = short_sale_circuit.data_provider.get_session
    stored = {}
    session = _Session()

    short_sale_circuit.data_provider.cache_get = lambda *args, **kwargs: None
    short_sale_circuit.data_provider.cache_put = lambda key, value: stored.update({key: value})
    short_sale_circuit.data_provider.get_session = lambda: session
    try:
        df = short_sale_circuit.fetch_short_sale_circuit_breakers(cache_age=0)
    finally:
        short_sale_circuit.data_provider.cache_get = old_cache_get
        short_sale_circuit.data_provider.cache_put = old_cache_put
        short_sale_circuit.data_provider.get_session = old_get_session

    assert len(df) == 2
    assert session.urls[0] == (short_sale_circuit.SHORT_SALE_CIRCUIT_PAGE_URL, 12)
    assert session.urls[1][0].endswith("/dynamic/symdir/shorthalts/shorthalts20260616.txt")
    assert "nasdaq_short_sale_circuit_breakers:v1" in stored
    assert stored["nasdaq_short_sale_circuit_breakers:v1"][0]["symbol"] == "DROP"


if __name__ == "__main__":
    test_parse_short_sale_circuit_file_handles_csv_quotes_and_timestamp()
    test_fetch_short_sale_circuit_breakers_uses_download_link_and_cache()
    print("2/2 short-sale circuit tests passed")

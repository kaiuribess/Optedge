# Purpose: Test FINRA short-interest parsing and caching.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engines.short_interest as short_interest


SAMPLE_FINRA_SI = """accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode|marketClassCode|currentShortPositionQuantity|previousShortPositionQuantity|stockSplitFlag|averageDailyVolumeQuantity|daysToCoverQuantity|revisionFlag|changePercent|changePreviousNumber|settlementDate
20260529|AAPL|Apple Inc.|Q|NASDAQ|1000|800||500|2.00||25.00|200|2026-05-29
20260529|SMOL|Small Cap Test Inc.|S|OTC|500000|250000||10000|50.00||100.00|250000|2026-05-29
"""


class _Response:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        if url.endswith("/files"):
            return _Response(
                '<a href="https://cdn.finra.org/equity/otcmarket/biweekly/shrt20260529.csv">'
                "May 29, 2026</a>"
            )
        return _Response(SAMPLE_FINRA_SI)


def test_parse_finra_short_interest_file():
    parsed = short_interest._parse_finra_short_interest(SAMPLE_FINRA_SI)
    assert parsed["AAPL"]["finra_short_interest_shares"] == 1000.0
    assert parsed["AAPL"]["finra_short_interest_prior_shares"] == 800.0
    assert parsed["AAPL"]["finra_short_interest_days_to_cover"] == 2.0
    assert parsed["AAPL"]["finra_short_interest_change_pct"] == 25.0
    assert parsed["SMOL"]["finra_short_interest_market_class"] == "OTC"


def test_process_ticker_uses_official_finra_when_short_info_missing():
    old_get_short_info = short_interest.data_provider.get_short_info
    try:
        short_interest.data_provider.get_short_info = lambda ticker: {}
        row = short_interest._process_ticker(
            "SMOL",
            finra_ratios={},
            finra_short_interest=short_interest._parse_finra_short_interest(SAMPLE_FINRA_SI),
        )
        assert row is not None
        assert row["shares_short"] == 500000.0
        assert row["short_ratio_days_to_cover"] == 50.0
        assert row["short_int_change_pct"] == 1.0
        assert row["short_int_score"] > 0
    finally:
        short_interest.data_provider.get_short_info = old_get_short_info


def test_fetch_finra_short_interest_uses_latest_download_and_cache():
    old_cache_get = short_interest.data_provider.cache_get
    old_cache_put = short_interest.data_provider.cache_put
    old_get_session = short_interest.data_provider.get_session
    captured = {}
    session = _Session()
    try:
        short_interest.data_provider.cache_get = lambda *args, **kwargs: None
        short_interest.data_provider.cache_put = lambda key, value: captured.update({key: value})
        short_interest.data_provider.get_session = lambda: session
        parsed = short_interest._fetch_finra_short_interest()
        assert parsed["AAPL"]["finra_short_interest_settlement_date"] == "2026-05-29"
        assert "finra_equity_short_interest_latest:v1" in captured
        assert any("shrt20260529.csv" in call[0] for call in session.calls)
    finally:
        short_interest.data_provider.cache_get = old_cache_get
        short_interest.data_provider.cache_put = old_cache_put
        short_interest.data_provider.get_session = old_get_session


if __name__ == "__main__":
    test_parse_finra_short_interest_file()
    test_process_ticker_uses_official_finra_when_short_info_missing()
    test_fetch_finra_short_interest_uses_latest_download_and_cache()
    print("3/3 short interest tests passed")

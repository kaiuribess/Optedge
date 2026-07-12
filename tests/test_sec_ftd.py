# Purpose: Test SEC failure-to-deliver normalization.
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import sec_ftd
from fusion import rank as fusion_rank


def test_sec_ftd_parser_and_summary_normalize_official_pipe_file():
    text = (
        "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE\n"
        "20260529|123456789|ABCD|250000|ABCD INC|2.50\n"
        "20260530|123456789|ABCD|500000|ABCD INC|3.00\n"
        "20260530|987654321|ZZZ|0|ZZZ INC|.\n"
    )
    parsed = sec_ftd._parse_ftd_text(text)
    assert list(parsed["ticker"]) == ["ABCD", "ABCD", "ZZZ"]
    assert parsed["sec_ftd_fails"].sum() == 750000

    row = sec_ftd._summarize_symbol("ABCD", parsed[parsed["ticker"] == "ABCD"])
    assert row["ticker"] == "ABCD"
    assert row["sec_ftd_latest_date"] == "2026-05-30"
    assert row["sec_ftd_fails"] == 500000
    assert row["sec_ftd_dollars"] == 1500000.0
    assert row["sec_ftd_score"] > 0
    assert "not proof" in row["sec_ftd_note"]


def test_sec_ftd_zip_link_parser_keeps_latest_page_order():
    html = """
    <a href="/files/data/fails-deliver-data/cnsfails202605b.zip">May second half</a>
    <a href="/files/data/fails-deliver-data/cnsfails202605a.zip">May first half</a>
    <a href="/files/data/fails-deliver-data/cnsfails202604b.zip">April second half</a>
    """
    urls = sec_ftd._latest_zip_urls(html, limit=2)
    assert urls == [
        "https://www.sec.gov/files/data/fails-deliver-data/cnsfails202605b.zip",
        "https://www.sec.gov/files/data/fails-deliver-data/cnsfails202605a.zip",
    ]


def test_sec_ftd_cached_zip_records_restore_date_types():
    old_cache_get = sec_ftd.data_provider.cache_get
    try:
        sec_ftd.data_provider.cache_get = lambda *args, **kwargs: [{
            "ticker": "ABCD",
            "settlement_date": "2026-05-30",
            "sec_ftd_fails": "500000",
            "sec_ftd_price": "3.00",
            "sec_ftd_description": "ABCD INC",
        }]
        frame = sec_ftd._fetch_zip_frame("https://example.test/cnsfails202605b.zip")
    finally:
        sec_ftd.data_provider.cache_get = old_cache_get

    row = sec_ftd._summarize_symbol("ABCD", frame)
    assert row["sec_ftd_latest_date"] == "2026-05-30"
    assert row["sec_ftd_fails"] == 500000
    assert row["sec_ftd_dollars"] == 1500000.0


def test_sec_ftd_context_flows_into_shares_and_futures_without_option_fields():
    ftd = pd.DataFrame([
        {
            "ticker": "ABCD",
            "sec_ftd_score": 2.0,
            "sec_ftd_latest_date": "2026-05-30",
            "sec_ftd_fails": 500000,
            "sec_ftd_dollars": 1500000.0,
            "sec_ftd_active_days": 2,
        },
        {
            "ticker": "SPY",
            "sec_ftd_score": 1.0,
            "sec_ftd_latest_date": "2026-05-30",
            "sec_ftd_fails": 100000,
            "sec_ftd_dollars": 5000000.0,
            "sec_ftd_active_days": 1,
        },
    ])
    old_min = fusion_rank.SHARES_MIN_SCORE
    try:
        fusion_rank.SHARES_MIN_SCORE = -999
        shares = fusion_rank.fuse_shares(
            ["ABCD", "WXYZ"],
            sentiment=pd.DataFrame(),
            fundamentals=pd.DataFrame(),
            insider=pd.DataFrame(),
            macro={"regime": "neutral", "macro_tilt": 0.0},
            sec_ftd=ftd,
        )
    finally:
        fusion_rank.SHARES_MIN_SCORE = old_min
    assert "sec_ftd_score" in shares.columns
    assert "z_sec_ftd" in shares.columns

    futures = pd.DataFrame([
        {"symbol": "ES=F", "name": "S&P 500 E-mini", "etf": "SPY", "futures_score": 1.0},
    ])
    enriched = fusion_rank.enrich_futures_context(
        futures,
        {"regime": "neutral", "macro_tilt": 0.0},
        sec_ftd=ftd,
    )
    assert "sec_ftd_score" in enriched.columns
    assert "z_context_sec_ftd" in enriched.columns
    assert enriched.loc[0, "sec_ftd_score"] == 1.0


if __name__ == "__main__":
    test_sec_ftd_parser_and_summary_normalize_official_pipe_file()
    test_sec_ftd_zip_link_parser_keeps_latest_page_order()
    test_sec_ftd_cached_zip_records_restore_date_types()
    test_sec_ftd_context_flows_into_shares_and_futures_without_option_fields()
    print("4/4 SEC FTD tests passed")

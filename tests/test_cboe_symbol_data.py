# Purpose: Test public Cboe option activity normalization.
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import cboe_symbol_data


def test_cboe_symbol_data_parser_normalizes_contract_rows():
    text = (
        "Option,Volume,Matched,Routed,Bid Size,Bid Price,Ask Size,Ask Price,Last Price\n"
        "AAPL Jan 15 200.0 Call,1234,1200,34,10,0.70,20,0.75,0.72\n"
        "SPY Jun 18 650.0 Put,500,450,50,5,1.10,6,1.15,1.12\n"
    )
    parsed = cboe_symbol_data.parse_symbol_data_csv(
        text,
        market="cone",
        asof=datetime(2026, 6, 17, tzinfo=timezone.utc),
    )
    assert list(parsed["ticker"]) == ["AAPL", "SPY"]
    assert parsed.loc[0, "expiry"] == "2027-01-15"
    assert parsed.loc[0, "option_side"] == "call"
    assert parsed.loc[0, "cboe_activity_volume"] == 1234
    assert parsed.loc[0, "cboe_activity_venue"] == "Cboe Options"


def test_cboe_symbol_data_aggregates_duplicate_contracts_across_venues():
    frame = pd.DataFrame([
        {
            "ticker": "AAPL",
            "expiry": "2027-01-15",
            "strike": 200.0,
            "option_side": "call",
            "cboe_activity_volume": 100,
            "cboe_activity_matched": 90,
            "cboe_activity_routed": 10,
            "cboe_activity_bid_size": 2,
            "cboe_activity_bid": 0.70,
            "cboe_activity_ask_size": 4,
            "cboe_activity_ask": 0.75,
            "cboe_activity_last": 0.72,
            "cboe_activity_contract": "AAPL Jan 15 200.0 Call",
            "cboe_activity_venue": "Cboe Options",
        },
        {
            "ticker": "AAPL",
            "expiry": "2027-01-15",
            "strike": 200.0,
            "option_side": "call",
            "cboe_activity_volume": 75,
            "cboe_activity_matched": 75,
            "cboe_activity_routed": 0,
            "cboe_activity_bid_size": 1,
            "cboe_activity_bid": 0.69,
            "cboe_activity_ask_size": 1,
            "cboe_activity_ask": 0.76,
            "cboe_activity_last": 0.71,
            "cboe_activity_contract": "AAPL Jan 15 200.0 Call",
            "cboe_activity_venue": "BZX Options",
        },
    ])
    out = cboe_symbol_data.aggregate_activity(frame)
    assert len(out) == 1
    assert out.loc[0, "cboe_activity_volume"] == 175
    assert out.loc[0, "cboe_activity_matched"] == 165
    assert out.loc[0, "cboe_activity_venues"] == "BZX Options,Cboe Options"


if __name__ == "__main__":
    test_cboe_symbol_data_parser_normalizes_contract_rows()
    test_cboe_symbol_data_aggregates_duplicate_contracts_across_venues()
    print("2/2 Cboe symbol data tests passed")

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.sec_filings as sec_filings


def _fact(value, end="2026-03-31", filed="2026-05-01", form="10-Q"):
    return {"val": value, "end": end, "filed": filed, "form": form, "fy": 2026, "fp": "Q1"}


def test_companyfacts_for_symbol_builds_metrics_and_watch_signals():
    old_ticker_map = sec_filings._ticker_map
    old_sec_get_json = sec_filings._sec_get_json
    try:
        sec_filings._ticker_map = lambda: {
            "TEST": {"ticker": "TEST", "cik": "0000001234", "name": "TEST CORP"}
        }
        sec_filings._sec_get_json = lambda *args, **kwargs: {
            "facts": {
                "us-gaap": {
                    "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [_fact(100)]}},
                    "Assets": {"units": {"USD": [_fact(1000)]}},
                    "Liabilities": {"units": {"USD": [_fact(900)]}},
                    "LongTermDebtAndFinanceLeaseObligationsCurrentAndNoncurrent": {
                        "units": {"USD": [_fact(500)]}
                    },
                    "Revenues": {"units": {"USD": [_fact(1000)]}},
                    "NetIncomeLoss": {"units": {"USD": [_fact(-25)]}},
                    "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [_fact(-10)]}},
                },
                "dei": {
                    "EntityCommonStockSharesOutstanding": {"units": {"shares": [_fact(50)]}},
                },
            }
        }

        report = sec_filings.companyfacts_for_symbol("TEST")
    finally:
        sec_filings._ticker_map = old_ticker_map
        sec_filings._sec_get_json = old_sec_get_json

    assert report["symbol"] == "TEST"
    assert report["count"] >= 7
    assert report["metrics"]["liabilities_to_assets"] == 0.9
    assert report["metrics"]["debt_to_assets"] == 0.5
    assert report["metrics"]["cash_to_debt"] == 0.2
    assert report["metrics"]["net_margin"] == -0.025
    assert report["metrics"]["cash_per_share"] == 2
    assert "high_liabilities_to_assets_watch" in report["watch_signals"]
    assert "low_cash_vs_debt_watch" in report["watch_signals"]
    assert "unprofitable_watch" in report["watch_signals"]
    assert "negative_operating_cash_flow_watch" in report["watch_signals"]


if __name__ == "__main__":
    test_companyfacts_for_symbol_builds_metrics_and_watch_signals()
    print("1/1 SEC companyfacts tests passed")

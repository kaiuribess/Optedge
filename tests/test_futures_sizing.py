# Purpose: Test futures and micro-contract sizing.
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.futures_sizing import add_sizing_to_futures  # noqa: E402


def test_futures_sizing_prefers_micro_when_full_contract_too_large():
    df = pd.DataFrame([{"symbol": "ES=F", "spot": 5000, "hv20": 0.20, "futures_score": 1.0}])
    out = add_sizing_to_futures(df, bankroll=10000)
    assert bool(out.loc[0, "using_micro"]) is True
    assert out.loc[0, "contract"] == "/MES"


def test_futures_sizing_marks_watch_when_even_micro_too_large():
    df = pd.DataFrame([{"symbol": "ES=F", "spot": 5000, "hv20": 3.0, "futures_score": 1.0}])
    out = add_sizing_to_futures(df, bankroll=1000)
    assert out.loc[0, "trade_status"] in {"Watch", "Skip"}
    assert int(out.loc[0, "suggested_contracts"]) == 0


if __name__ == "__main__":
    test_futures_sizing_prefers_micro_when_full_contract_too_large()
    test_futures_sizing_marks_watch_when_even_micro_too_large()
    print("2/2 futures sizing tests passed")

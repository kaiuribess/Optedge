import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtest import futures_positions


def _patch_files(td):
    futures_positions.OPEN_FILE = Path(td) / "open_futures_positions.json"
    futures_positions.CLOSED_FILE = Path(td) / "closed_futures_positions.json"
    futures_positions.DATA_DIR = Path(td)


def _signal(direction="long"):
    return pd.DataFrame([{
        "symbol": "ES=F",
        "name": "S&P 500",
        "direction": direction,
        "trade_status": "Trade",
        "entry_price": 100,
        "stop_price": 95 if direction == "long" else 105,
        "target_price": 110 if direction == "long" else 90,
        "point_value": 5,
        "suggested_contracts": 1,
        "futures_score": 1 if direction == "long" else -1,
    }])


def test_futures_long_closes_on_stop():
    with tempfile.TemporaryDirectory() as td:
        _patch_files(td)
        asof = datetime.now(timezone.utc)
        futures_positions.add_new_futures_signals(_signal("long"), asof)
        futures_positions._latest_price = lambda symbol: 94
        assert futures_positions.mark_to_market_futures(asof, None)["closed_this_iter"] == 1


def test_futures_short_closes_on_target():
    with tempfile.TemporaryDirectory() as td:
        _patch_files(td)
        asof = datetime.now(timezone.utc)
        futures_positions.add_new_futures_signals(_signal("short"), asof)
        futures_positions._latest_price = lambda symbol: 89
        assert futures_positions.mark_to_market_futures(asof, None)["closed_this_iter"] == 1


def test_futures_closes_on_score_reversal():
    with tempfile.TemporaryDirectory() as td:
        _patch_files(td)
        asof = datetime.now(timezone.utc)
        futures_positions.add_new_futures_signals(_signal("long"), asof)
        futures_positions._latest_price = lambda symbol: 101
        current = pd.DataFrame([{"symbol": "ES=F", "futures_score": -1.0}])
        assert futures_positions.mark_to_market_futures(asof, current)["closed_this_iter"] == 1


if __name__ == "__main__":
    test_futures_long_closes_on_stop()
    test_futures_short_closes_on_target()
    test_futures_closes_on_score_reversal()
    print("3/3 futures position tests passed")

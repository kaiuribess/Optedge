"""Historical backtest mode helpers."""
from __future__ import annotations

from backtest.historical import run_historical_backtest


def run(universe, factor_dfs):
    return run_historical_backtest(universe, factor_dfs)

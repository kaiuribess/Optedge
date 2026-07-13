# Purpose: Ensure look-ahead diagnostics cannot promote live trading models.
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from backtest import historical, model_accuracy


def test_historical_current_score_diagnostic_is_explicitly_ineligible():
    old_process = historical._process_ticker
    historical._process_ticker = lambda ticker: [
        {"ticker": ticker, "horizon_days": 7, "fwd_return": int(ticker[1:]) / 100.0}
    ]
    tickers = [f"T{index:02d}" for index in range(25)]
    factors = pd.DataFrame(
        {
            "ticker": tickers,
            "value_score": list(range(25)),
        }
    )
    try:
        report = historical.run_historical_backtest(
            tickers,
            {"value_score": factors},
            max_workers=2,
        )
    finally:
        historical._process_ticker = old_process

    assert report["evidence_status"] == "diagnostic_only_lookahead"
    assert report["eligible_for_model_promotion"] is False
    assert not report["ic"].empty
    assert report["ic"]["eligible_for_model_promotion"].eq(False).all()


def test_model_accuracy_refit_is_quarantined_before_loading_predictions():
    old_loader = model_accuracy._load_recent_predictions

    def fail_if_called(*args, **kwargs):
        raise AssertionError("unsafe scorer should not run by default")

    model_accuracy._load_recent_predictions = fail_if_called
    try:
        result = model_accuracy.refit_weights()
    finally:
        model_accuracy._load_recent_predictions = old_loader

    assert result is None


def test_opt_in_model_accuracy_diagnostic_cannot_mutate_production_artifacts(
    monkeypatch,
):
    with TemporaryDirectory(
        prefix="model-accuracy-",
        dir=Path(__file__).resolve().parent,
    ) as temp_dir:
        data_dir = Path(temp_dir) / "data"
        data_dir.mkdir()
        weights_file = data_dir / "model_weights.json"
        history_file = data_dir / "model_weights_history.jsonl"
        legacy_history_file = data_dir / "model_weight_history.json"
        weights_file.write_bytes(b'{"normal": {"bs": 1.0}}\n')
        history_file.write_bytes(b'{"ts": "production-history"}\n')
        legacy_history_file.write_bytes(b'{"history": "production"}\n')
        before = {path.name: path.read_bytes() for path in data_dir.iterdir()}

        predictions = pd.DataFrame(
            [
                {
                    "ticker": "TEST",
                    "asof": "2026-07-13T12:00:00+00:00",
                    "expiry": "2026-08-21",
                    "strike": 100.0,
                    "side": "call",
                    "regime": "normal",
                    "theo_bs": 1.8,
                    "theo_crr": 1.9,
                    "theo_bjs": 2.1,
                    "theo_cboe": 2.2,
                }
            ]
        )
        monkeypatch.setattr(model_accuracy, "DATA_DIR", data_dir)
        monkeypatch.setattr(model_accuracy, "WEIGHTS_FILE", weights_file)
        monkeypatch.setattr(model_accuracy, "WEIGHT_HISTORY_FILE", history_file)
        monkeypatch.setattr(
            model_accuracy,
            "_load_recent_predictions",
            lambda: predictions.copy(),
        )
        monkeypatch.setattr(
            model_accuracy,
            "_current_mid_for",
            lambda frame: frame.assign(market_mid_now=2.0),
        )

        result = model_accuracy.refit_weights(allow_lookahead_diagnostic=True)

        assert result is not None
        assert set(result["normal"]) == set(model_accuracy.MODELS)
        after = {path.name: path.read_bytes() for path in data_dir.iterdir()}
        assert after == before

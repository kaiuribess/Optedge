# Purpose: Test single-batch FinBERT ticker scoring.
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import finbert


def test_finbert_scores_all_tickers_in_one_batched_call():
    old_ensure = finbert._ensure_loaded
    old_score = finbert._score_texts
    old_device = finbert._DEVICE
    old_batch_env = os.environ.get("OPTEDGE_FINBERT_BATCH_SIZE")
    calls = []

    def fake_score(texts, batch_size=32):
        calls.append({"texts": list(texts), "batch_size": batch_size})
        return [0.25 for _ in texts]

    try:
        finbert._ensure_loaded = lambda: True
        finbert._score_texts = fake_score
        finbert._DEVICE = "cuda"
        os.environ["OPTEDGE_FINBERT_BATCH_SIZE"] = "7"
        df = pd.DataFrame([
            {"ticker": "AAPL", "top_headline": "Apple raises guidance"},
            {"ticker": "AAPL", "top_headline": "Apple demand improves"},
            {"ticker": "TSLA", "top_headline": "Tesla margins fall"},
        ])

        out = finbert.run(df, per_ticker_cap=5)
    finally:
        finbert._ensure_loaded = old_ensure
        finbert._score_texts = old_score
        finbert._DEVICE = old_device
        if old_batch_env is None:
            os.environ.pop("OPTEDGE_FINBERT_BATCH_SIZE", None)
        else:
            os.environ["OPTEDGE_FINBERT_BATCH_SIZE"] = old_batch_env

    assert len(calls) == 1
    assert calls[0]["batch_size"] == 7
    assert len(calls[0]["texts"]) == 3
    assert set(out["ticker"]) == {"AAPL", "TSLA"}
    assert int(out.loc[out["ticker"] == "AAPL", "finbert_n_headlines"].iloc[0]) == 2
    assert set(out["finbert_device"]) == {"cuda"}


if __name__ == "__main__":
    test_finbert_scores_all_tickers_in_one_batched_call()
    print("1/1 FinBERT batching tests passed")

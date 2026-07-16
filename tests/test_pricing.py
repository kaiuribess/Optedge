# Purpose: Test option pricing edge direction and sizing.
"""Unit tests for pricing models + Kelly formula - v20.7.

Run from the optedge/ root:
    python -m pytest tests/test_pricing.py -v
    OR
    python tests/test_pricing.py

These guard against regressions when numpy / scipy / transformers upgrade.
"""

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

try:
    import pytest  # noqa: F401 - optional, only needed for `pytest` runner
except ImportError:
    pytest = None  # Fall back to the __main__ runner at the bottom

import pandas as pd  # noqa: E402

from backtest.sizing import (  # noqa: E402
    _add_trade_status,
    add_directional_option_edges,
    add_sizing_to_options,
    add_sizing_to_shares,
    compute_option_ev_and_kelly,
)
from engines.mispricing import _pricing_edges  # noqa: E402
from pricing_models import (  # noqa: E402
    bjs_price,
    bs_delta,
    bs_implied_vol,
    bs_price,
    bs_price_vec,
    classify_vix_regime,
    crr_price,
    crr_price_vec,
    ensemble_theo,
    load_weights,
)


def test_share_sizing_freezes_reference_price_and_exit_geometry():
    shares = pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "spot": 100.0,
                "share_score": 1.0,
                "confidence": 70,
            }
        ]
    )

    sized = add_sizing_to_shares(shares, bankroll=10_000)

    assert sized.loc[0, "entry_price"] == 100.0
    assert sized.loc[0, "stop_price"] == 92.0
    assert sized.loc[0, "target_price"] == 120.0
    assert sized.loc[0, "stop_pct"] == -0.08
    assert sized.loc[0, "target_pct"] == 0.20


# ---------- Black-Scholes closed-form regressions ----------
def test_bs_atm_call_30d_20vol():
    # Reference: BS C(100, 100, 30/365, 4.5%, 20%, q=0) ≈ 2.47
    p = bs_price(100.0, 100.0, 30 / 365, 0.045, 0.20, 0.0, call=True)
    assert abs(p - 2.472) < 0.01, f"got {p}"


def test_bs_atm_put_30d_20vol():
    # Reference: BS P(100, 100, 30/365, 4.5%, 20%, q=0) ≈ 2.10
    p = bs_price(100.0, 100.0, 30 / 365, 0.045, 0.20, 0.0, call=False)
    assert abs(p - 2.102) < 0.01, f"got {p}"


def test_bs_call_intrinsic_at_expiry():
    # T -> 0, deep ITM call should be intrinsic value
    p = bs_price(150.0, 100.0, 1 / 365, 0.045, 0.20, 0.0, call=True)
    assert p >= 50.0, f"got {p}"


def test_bs_put_floor_at_zero():
    # Put on a tiny T, deep OTM, never goes negative
    p = bs_price(200.0, 100.0, 1 / 365, 0.045, 0.20, 0.0, call=False)
    assert p >= 0.0 and p < 0.5, f"got {p}"


def test_bs_implied_vol_round_trip():
    # Plug in BS price -> recover sigma
    spot, K, T, r, q = 100.0, 105.0, 60 / 365, 0.045, 0.0
    price = bs_price(spot, K, T, r, 0.30, q, call=True)
    iv = bs_implied_vol(price, spot, K, T, r, q, call=True)
    assert iv is not None and abs(iv - 0.30) < 0.01, f"got {iv}"


def test_bs_delta_atm_call_near_half():
    # Delta of ATM call ≈ 0.5 + small positive drift
    d = bs_delta(100.0, 100.0, 30 / 365, 0.045, 0.20, 0.0, call=True)
    assert 0.45 < d < 0.65, f"got {d}"


# ---------- American option models ----------
def test_crr_agrees_with_bs_on_no_div_call():
    # American call on non-dividend stock == European (no early exercise)
    args = (100.0, 100.0, 30 / 365, 0.045, 0.20, 0.0)
    bs = bs_price(*args, call=True)
    crr = crr_price(*args, call=True, steps=80)
    assert abs(bs - crr) < 0.05, f"bs={bs} crr={crr}"


def test_bjs_no_negative_prices():
    # Sweep strikes - every BJS price should be >= 0
    for K in [50, 80, 100, 120, 150, 200]:
        for call in (True, False):
            p = bjs_price(100.0, K, 30 / 365, 0.045, 0.30, 0.02, call=call)
            assert p >= 0, f"K={K} call={call} -> {p}"


def test_bjs_itm_put_above_intrinsic():
    # American ITM put should have value >= intrinsic
    S, K, T, r, sigma, q = 95.0, 100.0, 60 / 365, 0.045, 0.30, 0.02
    p = bjs_price(S, K, T, r, sigma, q, call=False)
    intrinsic = K - S
    assert p >= intrinsic - 0.01, f"p={p} intrinsic={intrinsic}"


# ---------- Vectorized pricing ----------
def test_bs_vec_matches_scalar():
    N = 50
    S = np.full(N, 100.0)
    K = np.linspace(70, 130, N)
    T = np.full(N, 30 / 365)
    sigma = np.full(N, 0.25)
    q = np.full(N, 0.01)
    mask = np.ones(N, dtype=bool)
    vec = bs_price_vec(S, K, T, 0.045, sigma, q, mask)
    for i in range(N):
        scalar = bs_price(
            float(S[i]), float(K[i]), float(T[i]), 0.045, float(sigma[i]), float(q[i]), call=True
        )
        assert abs(vec[i] - scalar) < 0.01, f"i={i}: vec={vec[i]} scalar={scalar}"


def test_crr_vec_matches_scalar_within_tolerance():
    N = 10
    S = np.full(N, 100.0)
    K = np.linspace(80, 120, N)
    T = np.full(N, 60 / 365)
    sigma = np.full(N, 0.30)
    q = np.full(N, 0.02)
    mask = np.zeros(N, dtype=bool)  # all puts
    vec = crr_price_vec(S, K, T, 0.045, sigma, q, mask, steps=60)
    for i in range(N):
        scalar = crr_price(
            float(S[i]),
            float(K[i]),
            float(T[i]),
            0.045,
            float(sigma[i]),
            float(q[i]),
            call=False,
            steps=60,
        )
        # Vectorized backward-induction differs from scalar at ~1e-3 due to
        # numerical accumulation order - accept 0.05 absolute tolerance
        assert abs(vec[i] - scalar) < 0.05, f"i={i}: vec={vec[i]} scalar={scalar}"


# ---------- Ensemble + regime ----------
def test_ensemble_theo_simple_average():
    out = ensemble_theo({"bs": 10.0, "crr": 10.0, "bjs": 10.0, "cboe": 10.0})
    assert abs(out - 10.0) < 0.01, f"got {out}"


def test_ensemble_theo_skips_nan():
    out = ensemble_theo({"bs": 10.0, "crr": float("nan"), "bjs": 12.0, "cboe": 10.0})
    assert out > 0 and not math.isnan(out), f"got {out}"


def test_regime_classification():
    assert classify_vix_regime(12.0) == "low_vol"
    assert classify_vix_regime(20.0) == "normal"
    assert classify_vix_regime(30.0) == "high_vol"
    assert classify_vix_regime(None) == "normal"
    assert classify_vix_regime(float("nan")) == "normal"


def test_load_weights_returns_valid_dict():
    for regime in ("low_vol", "normal", "high_vol"):
        w = load_weights(regime)
        assert isinstance(w, dict)
        assert abs(sum(w.values()) - 1.0) < 0.05, f"{regime}: {w}"
        for model, weight in w.items():
            assert 0 <= weight <= 1, f"{regime}/{model}: {weight}"


# ---------- Kelly + EV ----------
def test_kelly_zero_when_pred_negative():
    row = pd.Series({"pred_option_return_pct": -0.10, "delta": 0.40, "mid": 2.0, "dte": 30})
    out = compute_option_ev_and_kelly(row, aggressive=False, fill_slippage_pct=0.04)
    assert out["kelly_pct"] == 0.0


def test_kelly_zero_when_no_prediction():
    row = pd.Series({"pred_option_return_pct": float("nan"), "delta": 0.40, "mid": 2.0, "dte": 30})
    out = compute_option_ev_and_kelly(row, aggressive=False, fill_slippage_pct=0.04)
    assert math.isnan(out["kelly_pct"])


def test_slippage_subtracts_from_ev():
    # Same row, two slippage values - higher slippage = lower EV
    row = pd.Series({"pred_option_return_pct": 0.30, "delta": 0.40, "mid": 2.0, "dte": 30})
    ev_low = compute_option_ev_and_kelly(row, fill_slippage_pct=0.02)["ev_pct"]
    ev_high = compute_option_ev_and_kelly(row, fill_slippage_pct=0.08)["ev_pct"]
    assert ev_low > ev_high, f"ev_low={ev_low} ev_high={ev_high}"


def test_dte_discount_lowers_short_dte_prob_win():
    short = pd.Series({"pred_option_return_pct": 0.30, "delta": 0.40, "mid": 2.0, "dte": 5})
    long = pd.Series({"pred_option_return_pct": 0.30, "delta": 0.40, "mid": 2.0, "dte": 45})
    pw_short = compute_option_ev_and_kelly(short)["prob_win"]
    pw_long = compute_option_ev_and_kelly(long)["prob_win"]
    assert pw_short < pw_long, f"short={pw_short} long={pw_long}"


def test_conservative_kelly_prior_no_realized_data():
    # With realized_win_rate=None, avg_win should be the conservative prior
    row = pd.Series({"pred_option_return_pct": 0.20, "delta": 0.50, "mid": 3.0, "dte": 30})
    out = compute_option_ev_and_kelly(row, fill_slippage_pct=0.04)
    # Kelly fraction with avg_win=0.30 (conservative) should be < it would be
    # with avg_win = 0.40 (the v20.6 = max(0.50, abs(0.20)*2 - 0.04) = 0.36)
    assert out["kelly_pct"] >= 0
    assert out["kelly_pct"] < 0.5  # sanity: capped well below 50%


def test_pricing_edges_preserve_buyer_and_seller_direction():
    anomaly, buyer, seller = _pricing_edges(
        np.array([-0.20, 0.20, 0.00]),
        np.array([0.05, 0.05, 0.05]),
    )
    assert np.allclose(anomaly, [0.15, 0.15, -0.05])
    assert np.allclose(buyer, [0.15, -0.25, -0.05])
    assert np.allclose(seller, [-0.25, 0.15, -0.05])


def test_negative_buyer_edge_reduces_option_ev():
    common = {
        "pred_option_return_pct": 0.60,
        "delta": 0.75,
        "mid": 2.0,
        "dte": 45,
        "spread_pct": 0.02,
    }
    underpriced = compute_option_ev_and_kelly(
        pd.Series({**common, "buyer_edge_pct": 0.10}),
        fill_slippage_pct=0.04,
    )
    overpriced = compute_option_ev_and_kelly(
        pd.Series({**common, "buyer_edge_pct": -0.10}),
        fill_slippage_pct=0.04,
    )
    assert overpriced["pricing_edge_penalty_pct"] == 0.10
    assert underpriced["pricing_edge_penalty_pct"] == 0.0
    assert overpriced["ev_pct"] < underpriced["ev_pct"]
    assert overpriced["setup_quality_mult"] < underpriced["setup_quality_mult"]


def test_trade_status_requires_non_negative_buyer_edge_when_available():
    rows = pd.DataFrame(
        [
            {
                "ev_pct": 0.20,
                "kelly_pct": 0.02,
                "suggested_contracts": 1,
                "spread_pct": 0.02,
                "spread_to_edge_ratio": 0.20,
                "buyer_edge_pct": 0.10,
            },
            {
                "ev_pct": 0.20,
                "kelly_pct": 0.02,
                "suggested_contracts": 1,
                "spread_pct": 0.02,
                "spread_to_edge_ratio": 2.00,
                "buyer_edge_pct": -0.10,
            },
        ]
    )
    out = _add_trade_status(rows, asset="option")
    assert out.loc[0, "trade_status"] == "Trade"
    assert bool(out.loc[0, "pricing_edge_ok"])
    assert out.loc[0, "trade_gate_reason"] == "passed"
    assert out.loc[1, "trade_status"] == "Watch"
    assert not bool(out.loc[1, "pricing_edge_ok"])
    assert out.loc[1, "trade_gate_reason"] == "negative_buyer_edge_after_spread"


def test_legacy_option_snapshot_backfills_directional_edges():
    out = add_directional_option_edges(
        pd.DataFrame(
            [
                {"mispricing_pct": -0.20, "spread_pct": 0.05},
                {"mispricing_pct": 0.20, "spread_pct": 0.05},
            ]
        )
    )
    assert np.allclose(out["buyer_edge_pct"], [0.15, -0.25])
    assert np.allclose(out["seller_edge_pct"], [-0.25, 0.15])
    assert out["pricing_direction"].tolist() == [
        "underpriced_after_spread",
        "overpriced_after_spread",
    ]


def test_option_sizing_backfills_and_gates_overpriced_contracts():
    rows = pd.DataFrame(
        [
            {
                "ticker": "CHEAP",
                "mid": 1.0,
                "pred_option_return_pct": 2.0,
                "delta": 0.90,
                "dte": 45,
                "mispricing_pct": -0.20,
                "spread_pct": 0.02,
                "confidence": 80,
            },
            {
                "ticker": "RICH",
                "mid": 1.0,
                "pred_option_return_pct": 2.0,
                "delta": 0.90,
                "dte": 45,
                "mispricing_pct": 0.20,
                "spread_pct": 0.02,
                "confidence": 80,
            },
        ]
    )
    out = add_sizing_to_options(rows, bankroll=10_000, fill_slippage_pct=0.04)
    assert np.isclose(out.loc[0, "buyer_edge_pct"], 0.18)
    assert out.loc[0, "trade_status"] == "Trade"
    assert np.isclose(out.loc[1, "buyer_edge_pct"], -0.22)
    assert out.loc[1, "trade_status"] == "Watch"
    assert out.loc[1, "trade_gate_reason"] == "negative_buyer_edge_after_spread"


if __name__ == "__main__":
    # Allow `python tests/test_pricing.py` without pytest
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    print(f"Running {len(fns)} tests...")
    passed = 0
    failed = []
    for fn in fns:
        try:
            fn()
            print(f"  OK {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  X {fn.__name__}: {e}")
            failed.append(fn.__name__)
    print(f"\n{passed}/{len(fns)} passed", "OK" if not failed else f"- {len(failed)} failed")
    sys.exit(0 if not failed else 1)

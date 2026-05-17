"""Factor attribution — for each ranked trade, identify which 2-3 factors
contributed most to its score.

Used by the dashboard to add a 'why this ranked' chip on every card:
   📊 mispricing +0.45 · z_news +0.32 · z_value +0.21

This makes it possible to spot when a pick is one-trick (only mispricing
driving it, with all other signals neutral) vs multi-confirmed.
"""
from __future__ import annotations
from typing import Dict, List
import pandas as pd

# Z-columns in order of typical importance — used for display
DISPLAY_FACTORS = [
    ("z_mispricing", "mispricing"),
    ("z_iv_rank", "iv_rank"),
    ("z_value", "value"),
    ("z_fund", "fund"),
    ("z_news", "news"),
    ("z_sent", "sent"),
    ("z_earnings", "earn"),
    ("z_insider", "insider"),
    ("z_analyst", "analyst"),
    ("z_congress", "congress"),
    ("z_social", "social"),
    ("z_macro", "macro"),
    ("z_skew", "skew"),
]


def _weights() -> Dict[str, float]:
    """Pull current effective signal weights (runtime override → config default)."""
    try:
        from backtest.predictor import load_runtime_weights
        rt = load_runtime_weights()
        if rt:
            return rt
    except Exception:
        pass
    try:
        from config import SIGNAL_WEIGHTS
        return dict(SIGNAL_WEIGHTS)
    except Exception:
        return {}


# Map factor weight key → z-column it weights
_WEIGHT_KEY_TO_ZCOL = {
    "mispricing": "z_mispricing", "iv_rank": "z_iv_rank", "skew": "z_skew",
    "sentiment_d": "z_sent", "fundamentals": "z_fund", "insider": "z_insider",
    "macro": "z_macro", "news": "z_news", "earnings": "z_earnings",
    "value": "z_value", "congress": "z_congress", "social": "z_social",
    "analyst": "z_analyst",
}


def per_row_attribution(row: pd.Series, top_k: int = 3) -> List[Dict[str, float]]:
    """Return top-K (factor_name, signed_contribution) for one ranked row.

    contribution = weight × z-score. Positive contributions ranked first by magnitude.
    Skips zero/missing factors.
    """
    weights = _weights()
    if not weights:
        return []
    items = []
    for w_key, w_val in weights.items():
        zcol = _WEIGHT_KEY_TO_ZCOL.get(w_key)
        if zcol is None:
            continue
        z = row.get(zcol)
        if z is None or pd.isna(z) or z == 0:
            continue
        contribution = float(w_val) * float(z)
        if abs(contribution) < 0.01:
            continue
        # Side-align for puts: contributions are computed pre-side-mult in fusion,
        # so they're already the right sign for the displayed action.
        name = next((label for col, label in DISPLAY_FACTORS if col == zcol), w_key)
        items.append({"factor": name, "contribution": round(contribution, 3),
                       "z": round(float(z), 3), "weight": round(float(w_val), 3)})
    # Sort by absolute contribution descending, take top_k
    items.sort(key=lambda d: -abs(d["contribution"]))
    return items[:top_k]


def attribution_chip(row: pd.Series, top_k: int = 3) -> str:
    """Render a short HTML-safe attribution chip string for the card.

    Example output: "mispricing +0.45 · news +0.32 · value +0.21"
    """
    top = per_row_attribution(row, top_k=top_k)
    if not top:
        return ""
    return " · ".join(f"{d['factor']} {d['contribution']:+.2f}" for d in top)

# Purpose: Expected Value (EV) + Kelly Criterion position sizing.
"""Expected Value (EV) + Kelly Criterion position sizing.

Two related calculations per trade:

  1. **EV (% and $)** — what's the expected outcome of this trade?
     Long option (binary-ish):
       prob_win = |delta|  (proxy for P(ITM) at expiration)
       avg_win  = pred_option_return_pct (from the trained predictor)
       avg_loss = -1.0  (worst case: option expires worthless)
       ev_pct   = prob_win × avg_win + (1 - prob_win) × avg_loss
       ev_dollar = ev_pct × premium × 100  (per contract)

     Long shares:
       ev_pct = pred_stock_return_pct   (predictor — already a probability-weighted mean)
       ev_dollar = ev_pct × spot × shares

  2. **Kelly fraction** — what % of bankroll to risk?
     Standard Kelly: f* = (b·p - q) / b
       p = prob of profit
       q = 1 - p
       b = avg_win / avg_loss  (payoff ratio)
     We always use **quarter Kelly** (× 0.25) per the research consensus.
     Hard cap at 5% per option trade and 8% per share trade — single-trade risk limit.

Reference: research from FlashAlpha, Option Alpha, IntraAlpha — fractional Kelly
is the consensus approach for retail traders to avoid drawdowns.
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

log = logging.getLogger("optedge.sizing")

# Kelly fraction multipliers. Default = conservative quarter Kelly.
# Aggressive mode = half Kelly + larger caps + bigger total exposure.
KELLY_FRACTION = 0.25
MAX_PER_OPTION_TRADE = 0.05
MAX_PER_SHARE_TRADE = 0.08
TOTAL_OPTIONS_CAP = 0.30

# Aggressive overrides — set when --aggressive flag is passed
KELLY_FRACTION_AGGRESSIVE = 0.50
MAX_PER_OPTION_TRADE_AGGRESSIVE = 0.10
MAX_PER_SHARE_TRADE_AGGRESSIVE = 0.15
TOTAL_OPTIONS_CAP_AGGRESSIVE = 0.60
MIN_OPTION_BUYER_EDGE_PCT = 0.0

DEFAULT_BANKROLL = 10_000

# v20.7 — realistic fill cost. Retail options fills are typically 3-8% worse
# than the displayed mid (combined entry + exit spread crossing + market-maker
# edge). We apply this to EV and Kelly so the system isn't systematically
# optimistic. Default 4% can be overridden via config or env var.
try:
    from config import FILL_SLIPPAGE_PCT as _CFG_SLIPPAGE

    DEFAULT_FILL_SLIPPAGE_PCT = float(_CFG_SLIPPAGE)
except Exception:
    DEFAULT_FILL_SLIPPAGE_PCT = 0.04

# v20.7 — sector concentration limit. No more than this % of bankroll across
# all option positions in any single GICS sector (prevents 6 tech calls
# triggering at once).
DEFAULT_SECTOR_OPTIONS_CAP = 0.25
DEFAULT_SECTOR_OPTIONS_CAP_AGGRESSIVE = 0.40


def _bounded(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _option_buyer_edge(row: pd.Series) -> tuple[float, bool]:
    """Return buyer-directed edge and whether directional data was available."""
    raw = row.get("buyer_edge_pct")
    if raw is not None and not pd.isna(raw):
        return float(raw), True
    legacy = row.get("net_edge_pct")
    if legacy is not None and not pd.isna(legacy):
        return float(legacy), False
    return 0.0, False


def add_directional_option_edges(df: pd.DataFrame) -> pd.DataFrame:
    """Backfill buyer/seller edge fields on legacy option snapshots."""
    if df is None or df.empty or "mispricing_pct" not in df.columns:
        return df
    out = df.copy()
    mispricing = pd.to_numeric(out["mispricing_pct"], errors="coerce")
    spread = pd.to_numeric(
        out.get("spread_pct", pd.Series(0.0, index=out.index)),
        errors="coerce",
    ).fillna(0.0)
    anomaly = mispricing.abs() - spread
    buyer = -mispricing - spread
    seller = mispricing - spread
    for column, computed in (
        ("net_edge_pct", anomaly),
        ("buyer_edge_pct", buyer),
        ("seller_edge_pct", seller),
    ):
        if column not in out.columns:
            out[column] = computed
        else:
            out[column] = pd.to_numeric(out[column], errors="coerce").where(
                pd.to_numeric(out[column], errors="coerce").notna(),
                computed,
            )
    direction = pd.Series(
        np.select(
            [buyer > 0, seller > 0],
            ["underpriced_after_spread", "overpriced_after_spread"],
            default="inside_spread",
        ),
        index=out.index,
    )
    if "pricing_direction" not in out.columns:
        out["pricing_direction"] = direction
    else:
        existing = out["pricing_direction"].fillna("").astype(str).str.strip()
        out["pricing_direction"] = existing.where(existing.ne(""), direction)
    return out


def _option_setup_quality_mult(row: pd.Series) -> float:
    """Blend non-predictor evidence into position sizing.

    The predictor estimates return; this multiplier handles confirmation and
    tradeability from the broader stack: confidence, net edge, and spread.
    """
    mult = 1.0
    conf = row.get("confidence")
    if conf is not None and not pd.isna(conf):
        mult *= _bounded(float(conf) / 75.0, 0.60, 1.20)

    pricing_edge, has_pricing_edge = _option_buyer_edge(row)
    if has_pricing_edge or "net_edge_pct" in row.index:
        if pricing_edge <= 0:
            mult *= 0.65
        else:
            mult *= _bounded(1.0 + pricing_edge, 1.0, 1.25)

    spread = row.get("spread_pct")
    if spread is not None and not pd.isna(spread):
        if spread > 0.12:
            mult *= 0.65
        elif spread > 0.08:
            mult *= 0.80
        if has_pricing_edge or "net_edge_pct" in row.index:
            edge_for_cost = max(pricing_edge, 0.01)
            spread_to_edge = float(spread) / edge_for_cost
            if float(spread) > 0.05 and spread_to_edge > 1.5:
                mult *= 0.50

    return _bounded(mult, 0.25, 1.30)


def _share_setup_quality_mult(row: pd.Series) -> float:
    """Blend confidence and fused share score into share sizing."""
    mult = 1.0
    conf = row.get("confidence")
    if conf is not None and not pd.isna(conf):
        mult *= _bounded(float(conf) / 75.0, 0.60, 1.20)
    score = row.get("share_score")
    if score is not None and not pd.isna(score):
        mult *= _bounded(0.75 + float(score) * 0.20, 0.60, 1.25)
    return _bounded(mult, 0.25, 1.30)


def _add_trade_status(df: pd.DataFrame, asset: str) -> pd.DataFrame:
    """Attach Trade / Watch / Skip status after EV and sizing are known."""
    if df is None or df.empty:
        return df
    out = add_directional_option_edges(df.copy())
    ev = out.get("ev_pct", pd.Series(np.nan, index=out.index)).fillna(-999)
    kelly = out.get("kelly_pct", pd.Series(0.0, index=out.index)).fillna(0.0)
    if asset == "option":
        contracts = out.get("suggested_contracts", pd.Series(0, index=out.index)).fillna(0)
        ratio = out.get("spread_to_edge_ratio", pd.Series(0.0, index=out.index)).fillna(0.0)
        spread_bad = (ratio > 1.5) & (
            out.get("spread_pct", pd.Series(0.0, index=out.index)).fillna(0.0) > 0.05
        )
        if "buyer_edge_pct" in out.columns:
            buyer_edge = pd.to_numeric(out["buyer_edge_pct"], errors="coerce")
            buyer_edge_bad = buyer_edge.notna() & (buyer_edge < MIN_OPTION_BUYER_EDGE_PCT)
        else:
            buyer_edge_bad = pd.Series(False, index=out.index)
        trade = (ev > 0) & (kelly > 0) & (contracts > 0) & ~spread_bad & ~buyer_edge_bad
    else:
        dollars = out.get("suggested_dollars", pd.Series(0.0, index=out.index)).fillna(0.0)
        spread_bad = pd.Series(False, index=out.index)
        trade = (ev > 0) & (kelly > 0) & (dollars > 0)
    watch = (ev > 0) & ~trade & ~spread_bad
    out["trade_status"] = np.where(trade, "Trade", np.where(watch, "Watch", "Skip"))
    out["is_actionable"] = trade
    if asset == "option":
        out["pricing_edge_ok"] = ~buyer_edge_bad
        out["trade_gate_reason"] = np.select(
            [
                ev <= 0,
                kelly <= 0,
                contracts <= 0,
                buyer_edge_bad,
                spread_bad,
            ],
            [
                "non_positive_ev",
                "zero_kelly",
                "zero_contracts",
                "negative_buyer_edge_after_spread",
                "spread_exceeds_edge",
            ],
            default="passed",
        )
    return out


def add_pre_guard_qualification(df: pd.DataFrame, asset: str) -> pd.DataFrame:
    """Freeze strategy qualification before portfolio-level research guards.

    A blocked guard must prevent execution without preventing the paper research
    stream from learning whether the current entry rules would have worked.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    status = out.get("trade_status", pd.Series("", index=out.index)).fillna("").astype(str)
    actionable = (
        out.get("is_actionable", pd.Series(False, index=out.index)).fillna(False).astype(bool)
    )
    out["pre_guard_trade_status"] = status
    out["pre_guard_is_actionable"] = actionable
    if asset in {"option", "futures"}:
        size = pd.to_numeric(
            out.get("suggested_contracts", pd.Series(0.0, index=out.index)),
            errors="coerce",
        ).fillna(0.0)
        out["pre_guard_suggested_contracts"] = size
    else:
        size = pd.to_numeric(
            out.get("suggested_dollars", pd.Series(0.0, index=out.index)),
            errors="coerce",
        ).fillna(0.0)
        out["pre_guard_suggested_dollars"] = size
    out["strategy_qualified_pre_guard"] = status.str.lower().eq("trade") & actionable & (size > 0)
    return out


def get_sizing_params(aggressive: bool = False):
    """Return (kelly_fraction, max_option_pct, max_share_pct, total_cap)."""
    if aggressive:
        return (
            KELLY_FRACTION_AGGRESSIVE,
            MAX_PER_OPTION_TRADE_AGGRESSIVE,
            MAX_PER_SHARE_TRADE_AGGRESSIVE,
            TOTAL_OPTIONS_CAP_AGGRESSIVE,
        )
    return (KELLY_FRACTION, MAX_PER_OPTION_TRADE, MAX_PER_SHARE_TRADE, TOTAL_OPTIONS_CAP)


def _time_of_day_kelly_mult() -> float:
    """Discount Kelly during ET market windows with chronically wide spreads.

    9:30-10:00 ET — opening rotation, wide auction spreads → 0.7×
    15:30-16:00 ET — closing rotation, MOC imbalance → 0.7×
    Outside RTH (4pm-9:30am ET) — stale quotes, very wide → 0.5×
    Prime hours 10am-3:30pm ET → 1.0× (no discount)
    """
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        now = _dt.now(ZoneInfo("US/Eastern"))
    except Exception:
        return 1.0  # if tz lib missing, don't penalize
    weekday = now.weekday()
    if weekday >= 5:  # Sat/Sun = totally stale
        return 0.5
    h, m = now.hour, now.minute
    minutes = h * 60 + m
    if minutes < 9 * 60 + 30 or minutes >= 16 * 60:
        return 0.5  # outside RTH
    if 9 * 60 + 30 <= minutes < 10 * 60:
        return 0.7  # opening 30min
    if 15 * 60 + 30 <= minutes < 16 * 60:
        return 0.7  # closing 30min
    return 1.0  # prime time


def _earnings_iv_crush_mult(row: pd.Series) -> float:
    """If earnings falls between today and option expiry, apply IV crush discount.

    Returns a multiplier in [0.5, 1.0] applied to predicted return BEFORE Kelly.
    Closer earnings = bigger crush.
    """
    days_to_earn = row.get("days_to_earnings")
    dte = row.get("dte")
    if days_to_earn is None or pd.isna(days_to_earn):
        return 1.0
    try:
        days_to_earn = int(days_to_earn)
        dte = int(dte) if dte is not None and not pd.isna(dte) else 0
    except Exception:
        return 1.0
    if not (0 <= days_to_earn <= dte):
        return 1.0
    if days_to_earn <= 2:
        return 0.50
    if days_to_earn <= 7:
        return 0.65
    if days_to_earn <= 14:
        return 0.80
    return 0.90


def compute_option_ev_and_kelly(
    row: pd.Series,
    aggressive: bool = False,
    fill_slippage_pct: float = None,
    realized_win_rate: float = None,
) -> dict[str, float]:
    """Per-row EV and Kelly fraction for a long option.

    v20.7 changes:
      - Apply realistic fill slippage (default 4%) — every retail option fill
        is worse than mid; the system was systematically optimistic.
      - prob_win: |delta| is P(ITM at expiration) but short-DTE OTM options
        rarely become winners after theta + spread. Apply a DTE-aware discount
        so a 0.10-delta 5-DTE call doesn't get a 0.10 prob_win attached.
      - Less-optimistic Kelly prior: avg_win = max(0.30, abs(pred) * 1.3)
        until realized P&L data justifies a higher prior. Switches to a
        percentile of realized winner P&L once `realized_win_rate` is supplied.
    """
    if fill_slippage_pct is None:
        fill_slippage_pct = DEFAULT_FILL_SLIPPAGE_PCT
    pred_raw = row.get("pred_option_return_pct")
    has_prediction = pred_raw is not None and not (
        isinstance(pred_raw, float) and math.isnan(pred_raw)
    )
    pred = float(pred_raw or 0)
    crush_mult = _earnings_iv_crush_mult(row)
    tod_mult = _time_of_day_kelly_mult()
    pred = pred * crush_mult * tod_mult
    # v20.7: subtract round-trip fill cost from predicted return BEFORE EV/Kelly
    pred_net = pred - fill_slippage_pct
    spread = float(row.get("spread_pct") or 0.0)
    buyer_edge, has_directional_edge = _option_buyer_edge(row)
    edge_for_cost = max(buyer_edge, 0.0) if has_directional_edge else abs(buyer_edge)
    spread_to_edge = spread / max(edge_for_cost, 0.01)
    pricing_edge_penalty = 0.0
    if has_directional_edge and buyer_edge < MIN_OPTION_BUYER_EDGE_PCT:
        pricing_edge_penalty = min(0.25, abs(buyer_edge - MIN_OPTION_BUYER_EDGE_PCT))
        pred_net -= pricing_edge_penalty
    spread_penalty = 0.0
    if spread > 0.05 and spread_to_edge > 1.0:
        spread_penalty = min(0.25, max(0.0, spread - edge_for_cost))
        pred_net -= spread_penalty

    delta = float(row.get("delta") or 0.5)
    mid = float(row.get("mid") or 0)
    dte = float(row.get("dte") or 30)
    # v20.7: DTE-aware P(ITM at expiry) discount. For short DTEs, |delta| is
    # too optimistic relative to "actually profitable after costs".
    dte_factor = max(0.5, min(1.0, dte / 30.0))
    prob_win = max(0.05, min(0.90, abs(delta) * dte_factor))

    if mid <= 0 or not has_prediction:
        return {
            "ev_pct": float("nan"),
            "ev_dollar_per_contract": float("nan"),
            "kelly_full": float("nan"),
            "kelly_pct": float("nan"),
            "prob_win": prob_win,
            "spread_to_edge_ratio": spread_to_edge,
            "liquidity_penalty_pct": spread_penalty,
            "pricing_edge_penalty_pct": pricing_edge_penalty,
        }

    ev_pct = pred_net
    ev_dollar = ev_pct * mid * 100

    if pred_net <= 0:
        return {
            "ev_pct": ev_pct,
            "ev_dollar_per_contract": ev_dollar,
            "kelly_full": 0.0,
            "kelly_pct": 0.0,
            "prob_win": prob_win,
            "spread_to_edge_ratio": spread_to_edge,
            "liquidity_penalty_pct": spread_penalty,
            "pricing_edge_penalty_pct": pricing_edge_penalty,
        }

    # v20.7: conservative Kelly prior. Until we have 500+ logged signals AND
    # 10+ days of forward P&L, use a less-optimistic avg_win.
    if realized_win_rate is not None and 0 < realized_win_rate < 1:
        avg_win = max(0.30, abs(pred_net) * 1.5)
    else:
        avg_win = max(0.30, abs(pred_net) * 1.3)  # was 0.50, abs(pred)*2
    avg_loss = 0.60
    b = avg_win / avg_loss
    p = prob_win
    q = 1 - p
    kelly_full = max(0.0, (b * p - q) / b)
    frac, cap, _, _ = get_sizing_params(aggressive)
    setup_mult = _option_setup_quality_mult(row)
    kelly_quarter = min(kelly_full * frac * setup_mult, cap)

    return {
        "ev_pct": ev_pct,
        "ev_dollar_per_contract": ev_dollar,
        "kelly_full": kelly_full,
        "kelly_pct": kelly_quarter,
        "prob_win": prob_win,
        "fill_slippage_pct": fill_slippage_pct,
        "setup_quality_mult": setup_mult,
        "spread_to_edge_ratio": spread_to_edge,
        "liquidity_penalty_pct": spread_penalty,
        "pricing_edge_penalty_pct": pricing_edge_penalty,
    }


def compute_share_ev_and_kelly(
    row: pd.Series, hv_proxy: float = 0.30, aggressive: bool = False
) -> dict[str, float]:
    """Per-row EV and Kelly fraction for a long share buy."""
    pred_raw = row.get("pred_stock_return_pct")
    has_prediction = pred_raw is not None and not (
        isinstance(pred_raw, float) and math.isnan(pred_raw)
    )
    if not has_prediction:
        return {"ev_pct": float("nan"), "kelly_full": float("nan"), "kelly_pct": float("nan")}
    pred = float(pred_raw or 0)
    if pred <= 0:
        return {"ev_pct": pred, "kelly_full": 0.0, "kelly_pct": 0.0}

    # For shares, use a more graceful loss model: realized vol acts as the std-dev
    # of outcomes. Average loser ~ 1×hv (one standard-deviation move down over the holding period).
    hv = hv_proxy  # default 30% annualized vol
    # Probability of profit estimated from predicted return / vol via normal approx
    prob_win = 0.5 + max(-0.4, min(0.4, pred / max(hv, 0.05)))
    avg_win = max(pred, hv * 0.5)  # bullish baseline
    avg_loss = hv * 0.5  # 0.5 sigma down move proxy

    ev_pct = prob_win * avg_win - (1 - prob_win) * avg_loss

    b = avg_win / avg_loss if avg_loss > 0 else 1.0
    kelly_full = (b * prob_win - (1 - prob_win)) / b
    kelly_full = max(0.0, kelly_full)
    frac, _, share_cap, _ = get_sizing_params(aggressive)
    setup_mult = _share_setup_quality_mult(row)
    kelly_quarter = min(kelly_full * frac * setup_mult, share_cap)

    return {
        "ev_pct": ev_pct,
        "kelly_full": kelly_full,
        "kelly_pct": kelly_quarter,
        "prob_win": prob_win,
        "setup_quality_mult": setup_mult,
    }


def _apply_sector_cap(df: pd.DataFrame, bankroll: float, sector_cap_pct: float) -> pd.DataFrame:
    """v20.7 — sector concentration cap. Walks the ranked options (highest
    Kelly first) and zeros out positions that would exceed `sector_cap_pct`
    of bankroll in any one sector. Sector comes from `classification` or
    `sector` column (already populated by fundamentals/value engines).

    Without a known sector, contracts pass through uncapped. We never SHRINK
    existing positions — we just stop accepting new ones once a sector tank
    is full, prioritising by Kelly size."""
    if df is None or df.empty:
        return df
    sector_col = (
        "sector"
        if "sector" in df.columns
        else ("classification" if "classification" in df.columns else None)
    )
    if sector_col is None:
        return df
    if "kelly_pct" not in df.columns or "actual_dollars" not in df.columns:
        return df

    sector_budget = bankroll * sector_cap_pct
    sector_used: dict = {}
    out = df.copy()
    # Rank by kelly_pct descending so the strongest signals get sector capacity first
    order = out.sort_values("kelly_pct", ascending=False).index
    capped_idx = []
    for idx in order:
        sec = out.at[idx, sector_col]
        if pd.isna(sec) or not sec:
            continue
        spend = float(out.at[idx, "actual_dollars"] or 0)
        used = sector_used.get(sec, 0.0)
        if used + spend > sector_budget:
            # Cap this position to zero — don't double-down on a saturated sector
            out.at[idx, "kelly_pct"] = 0.0
            out.at[idx, "suggested_dollars"] = 0
            out.at[idx, "suggested_contracts"] = 0
            out.at[idx, "actual_dollars"] = 0
            capped_idx.append(idx)
        else:
            sector_used[sec] = used + spend
    if capped_idx:
        out["sector_cap_applied"] = out.index.isin(capped_idx)
        log.info(
            "sector cap: dropped %d options to keep each sector ≤ %.0f%% of bankroll",
            len(capped_idx),
            sector_cap_pct * 100,
        )
    return out


def add_sizing_to_options(
    df: pd.DataFrame,
    bankroll: float = DEFAULT_BANKROLL,
    aggressive: bool = False,
    drawdown_mult: float = 1.0,
    fill_slippage_pct: float = None,
    sector_cap_pct: float = None,
) -> pd.DataFrame:
    """Compute EV + Kelly + suggested dollars + contract count + exit triggers.

    v20: ``drawdown_mult`` is applied to kelly_pct (default 1.0 = no effect).
    v20.7: ``fill_slippage_pct`` and ``sector_cap_pct`` for realistic costs.
    """
    if df is None or df.empty:
        return df
    out = add_directional_option_edges(df)
    rows = [
        compute_option_ev_and_kelly(r, aggressive=aggressive, fill_slippage_pct=fill_slippage_pct)
        for _, r in out.iterrows()
    ]
    res = pd.DataFrame(rows, index=out.index)
    out = pd.concat([out, res], axis=1)
    # v20 — apply drawdown breaker multiplier
    if drawdown_mult != 1.0:
        out["kelly_pct"] = out["kelly_pct"] * drawdown_mult
        out["drawdown_mult"] = drawdown_mult
    out["suggested_dollars"] = (out["kelly_pct"] * bankroll).round(0)
    cost_per_contract = (out["mid"].fillna(0) * 100).replace(0, np.nan)
    out["suggested_contracts"] = (
        np.floor(out["suggested_dollars"] / cost_per_contract).fillna(0).astype(int)
    )
    out["actual_dollars"] = out["suggested_contracts"] * cost_per_contract.fillna(0)
    # v20.7 sector concentration cap — applied AFTER raw sizing so we know
    # the dollar exposure per contract before deciding whether the sector is full
    if sector_cap_pct is None:
        sector_cap_pct = (
            DEFAULT_SECTOR_OPTIONS_CAP_AGGRESSIVE if aggressive else DEFAULT_SECTOR_OPTIONS_CAP
        )
    out = _apply_sector_cap(out, bankroll, sector_cap_pct)
    # Exit triggers — simple, robust rules
    out["stop_price"] = (out["mid"].fillna(0) * 0.50).round(2)
    out["target_price"] = (out["mid"].fillna(0) * 2.00).round(2)
    return _add_trade_status(out, asset="option")


def add_sizing_to_shares(
    df: pd.DataFrame,
    bankroll: float = DEFAULT_BANKROLL,
    aggressive: bool = False,
    drawdown_mult: float = 1.0,
) -> pd.DataFrame:
    """Compute EV + Kelly + suggested dollars + exit triggers for shares.

    v20: ``drawdown_mult`` applies the breaker multiplier (default 1.0).
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    rows = [compute_share_ev_and_kelly(r, aggressive=aggressive) for _, r in out.iterrows()]
    res = pd.DataFrame(rows, index=out.index)
    out = pd.concat([out, res], axis=1)
    if drawdown_mult != 1.0:
        out["kelly_pct"] = out["kelly_pct"] * drawdown_mult
        out["drawdown_mult"] = drawdown_mult
    out["suggested_dollars"] = (out["kelly_pct"] * bankroll).round(0)
    # Stop -8% from current, target +20% (aggressive: -10%/+30%)
    stop_pct = -0.10 if aggressive else -0.08
    target_pct = 0.30 if aggressive else 0.20
    out["stop_pct"] = stop_pct
    out["target_pct"] = target_pct
    existing_entry = (
        pd.to_numeric(out["entry_price"], errors="coerce")
        if "entry_price" in out.columns
        else pd.Series(np.nan, index=out.index, dtype=float)
    )
    spot = (
        pd.to_numeric(out["spot"], errors="coerce")
        if "spot" in out.columns
        else pd.Series(np.nan, index=out.index, dtype=float)
    )
    reference_entry = existing_entry.where(existing_entry > 0).combine_first(spot.where(spot > 0))
    out["entry_price"] = reference_entry
    out["stop_price"] = (reference_entry * (1.0 + stop_pct)).round(2)
    out["target_price"] = (reference_entry * (1.0 + target_pct)).round(2)
    return _add_trade_status(out, asset="shares")


def sort_for_trade_selection(df: pd.DataFrame, asset: str = "option") -> pd.DataFrame:
    """Sort cards by actionability first, then EV/Kelly/confluence.

    This keeps the research score available, but the visible buy list is led
    by ideas that the full sizing stack can actually support.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    ev = out.get("ev_pct", pd.Series(0.0, index=out.index)).fillna(0.0)
    kelly = out.get("kelly_pct", pd.Series(0.0, index=out.index)).fillna(0.0)
    conf = out.get("confidence", pd.Series(50.0, index=out.index)).fillna(50.0) / 100.0
    setup = out.get("setup_quality_mult", pd.Series(1.0, index=out.index)).fillna(1.0)
    actionable = (
        out.get("is_actionable", pd.Series(False, index=out.index)).fillna(False).astype(bool)
    )
    base_score = out.get(
        "rank_score", out.get("fused_score", pd.Series(0.0, index=out.index))
    ).fillna(0.0)
    out["trade_score"] = (
        actionable.astype(float) * 1000.0
        + np.maximum(ev, 0.0) * 100.0
        + kelly * 100.0
        + conf * 10.0
        + setup
        + base_score * 0.01
    )
    return out.sort_values("trade_score", ascending=False).reset_index(drop=True)

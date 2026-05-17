"""Signal fusion + ranking.

Long-only philosophy:
  - Buy calls when underlying multi-factor view is BULLISH and the call
    is at-or-below fair value
  - Buy puts when underlying multi-factor view is BEARISH and the put
    is at-or-below fair value
  - Buy shares for small caps when multi-factor view is bullish but
    options aren't liquid enough to trade

No selling, no credit spreads, no shorting. Maximum diversity (1 idea/ticker).
"""
from __future__ import annotations
import logging
from typing import Dict, Any, List
import math

import numpy as np
import pandas as pd

from config import (
    SIGNAL_WEIGHTS, TOP_N_CALLS, TOP_N_PUTS, TOP_N_SHARES,
    MAX_PER_TICKER, SHARES_MIN_SCORE,
)
from utils import zscore, winsor, safe_int

log = logging.getLogger("optedge.fusion")


# -------- Helpers ------------------------------------------------------
def _confidence(aligned_score: float) -> int:
    """Map an action-aligned score to 0–100. Positive aligned = high conf."""
    return int(max(0, min(100, 50 + aligned_score * 22)))


def _option_signal_label(side: str, vol_premium: float) -> str:
    if side == "call":
        return "Long Call (cheap)" if vol_premium < 0 else "Long Call (fair)"
    return "Long Put (cheap)" if vol_premium < 0 else "Long Put (fair)"


def _option_reasoning(row: pd.Series) -> str:
    pieces = []
    vp = row.get("vol_premium", 0.0)
    pieces.append(
        f"IV {row['iv_market']*100:.1f}% vs HV30 {row['fair_vol']*100:.1f}% "
        f"({'cheap' if vp < 0 else 'rich'} by {abs(vp)*100:.1f} vol pts)."
    )
    miss_pct = row.get("mispricing_pct", 0.0) * 100
    if abs(miss_pct) > 3:
        pieces.append(f"Market mid is {miss_pct:+.1f}% vs theo (BS @ HV30).")
    iv_rank = row.get("iv_rank")
    if iv_rank is not None and not (isinstance(iv_rank, float) and math.isnan(iv_rank)):
        pieces.append(f"IV-rank proxy {iv_rank:.0f}.")
    skew = row.get("skew_25d")
    if skew is not None and not (isinstance(skew, float) and math.isnan(skew)):
        pieces.append(f"25Δ skew (puts−calls): {skew*100:+.1f} vol pts.")
    s_d = row.get("sentiment_delta", 0.0) or 0.0
    mentions = row.get("mentions", 0) or 0
    if mentions and abs(s_d) > 0.05:
        direction = "rising" if s_d > 0 else "falling"
        pieces.append(f"Reddit Δsentiment {direction} ({s_d:+.2f}, {int(mentions)} mentions).")
    fund = row.get("fund_score")
    if fund is not None and abs(fund) > 0.5:
        cls = row.get("classification", "")
        pieces.append(f"Fundamentals {fund:+.1f} ({cls}).")
    insider = row.get("insider_score", 0) or 0
    if abs(insider) > 0.5:
        n_buys = safe_int(row.get("n_buys"))
        n_sells = safe_int(row.get("n_sells"))
        pieces.append(f"Insider net {insider:+.1f} ({n_buys}P / {n_sells}S, last 90d).")
    # News
    n24 = row.get("n_24h", 0) or 0
    nd = row.get("news_delta", 0) or 0
    if n24 >= 1 and abs(nd) > 0.05:
        direction = "positive" if nd > 0 else "negative"
        pieces.append(f"News: {int(n24)} headlines/24h, sentiment {direction} ({nd:+.2f}).")
    # Earnings catalyst
    dte_e = row.get("days_to_earnings")
    if dte_e is not None and 0 <= dte_e <= 30:
        sup = row.get("last_eps_surprise_pct")
        sup_str = f", last EPS {sup*100:+.1f}%" if sup is not None else ""
        pieces.append(f"Earnings in {int(dte_e)}d{sup_str}.")
    macro_tilt = row.get("macro_tilt", 0.0)
    regime = row.get("regime", "neutral")
    pieces.append(f"Macro: {regime} (tilt {macro_tilt:+.2f}).")
    return " ".join(pieces)


def _option_risks(row: pd.Series) -> str:
    risks = []
    if row.get("spread_pct", 0) > 0.08:
        risks.append(f"Wide spread {row['spread_pct']*100:.1f}%")
    if row.get("dte", 0) <= 21:
        risks.append("Short DTE: theta decay accelerates")
    if row.get("open_interest", 0) < 500:
        risks.append("Low OI: exit liquidity risk")
    earn = row.get("earnings_date")
    if earn:
        risks.append(f"Earnings {earn} — IV crush risk")
    if row.get("regime") == "risk_off" and row["side"] == "call":
        risks.append("Long call against risk-off macro")
    if row.get("regime") == "risk_on" and row["side"] == "put":
        risks.append("Long put against risk-on macro")
    if row.get("vol_premium", 0) > 0:
        risks.append("Buying above fair vol — needs realised vol to rise")
    if not risks:
        risks.append("Standard option drawdown risk")
    return "; ".join(risks)


def _share_reasoning(row: pd.Series) -> str:
    pieces = []
    s_d = row.get("sentiment_delta", 0.0) or 0.0
    mentions = row.get("mentions", 0) or 0
    if mentions and s_d > 0.05:
        pieces.append(f"Reddit Δsentiment rising (+{s_d:.2f}, {safe_int(mentions)} mentions, velocity {safe_int(row.get('velocity')):+d}).")
    fund = row.get("fund_score") or 0
    if fund > 0.3:
        cls = row.get("classification", "")
        pieces.append(f"Fundamentals +{fund:.1f} ({cls}, rev growth {(row.get('rev_growth') or 0)*100:+.0f}%).")
    insider = row.get("insider_score") or 0
    if insider > 0.5:
        n_buys = safe_int(row.get("n_buys"))
        pieces.append(f"Insider net buying +{insider:.1f} ({n_buys} open-market purchases).")
    # News
    n24 = row.get("n_24h", 0) or 0
    nd = row.get("news_delta", 0) or 0
    if n24 >= 1 and nd > 0.05:
        pieces.append(f"News: {int(n24)} headlines/24h, sentiment +{nd:.2f}.")
    # Earnings
    dte_e = row.get("days_to_earnings")
    if dte_e is not None and 0 <= dte_e <= 30:
        pieces.append(f"Earnings in {int(dte_e)}d.")
    pieces.append(f"Macro: {row.get('regime','neutral')} (tilt {row.get('macro_tilt',0):+.2f}).")
    return " ".join(pieces) or "Multi-factor bullish signal."


def _share_risks(row: pd.Series) -> str:
    risks = []
    mcap = row.get("market_cap")
    if mcap and mcap < 1e9:
        risks.append("Sub-$1B mcap: thin liquidity")
    cls = row.get("classification") or ""
    if cls == "distressed":
        risks.append("Distressed financials — binary outcome risk")
    if cls == "speculative":
        risks.append("Speculative classification — narrative-driven")
    earn = row.get("earnings_date")
    if earn:
        risks.append(f"Earnings {earn}")
    if row.get("regime") == "risk_off":
        risks.append("Small-cap long against risk-off macro")
    if not risks:
        risks.append("Standard equity drawdown risk")
    return "; ".join(risks)


# -------- Joining ------------------------------------------------------
def _safe_merge_score(df: pd.DataFrame, other: pd.DataFrame,
                       score_col: str, extra_cols: List[str] = None,
                       fill_value: float = 0.0) -> pd.DataFrame:
    """v20 helper — merge a simple {ticker, score_col, extra_cols} engine
    output into df with NaN-fill. Used for all new v20 factors that follow
    the broadcast-or-per-ticker pattern.
    """
    if df is None or df.empty:
        return df
    extra_cols = extra_cols or []
    if other is None or other.empty:
        df[score_col] = fill_value
        for c in extra_cols:
            if c not in df.columns:
                df[c] = None
        return df
    keep = ["ticker", score_col] + [c for c in extra_cols if c in other.columns]
    keep = [c for c in keep if c in other.columns]
    if "ticker" not in keep or score_col not in keep:
        df[score_col] = fill_value
        return df
    df = df.merge(other[keep], on="ticker", how="left", suffixes=("", "_dup"))
    df[score_col] = pd.to_numeric(df[score_col], errors="coerce").fillna(fill_value)
    return df


def _join_per_ticker(contracts: pd.DataFrame, mp_summary: pd.DataFrame,
                     sentiment: pd.DataFrame, fundamentals: pd.DataFrame,
                     insider: pd.DataFrame, macro: Dict[str, Any],
                     news: pd.DataFrame = None, earnings: pd.DataFrame = None,
                     congress: pd.DataFrame = None,
                     social: pd.DataFrame = None,
                     analyst: pd.DataFrame = None,
                     uoa: pd.DataFrame = None,
                     sector_rs: pd.DataFrame = None,
                     dark_pool: pd.DataFrame = None,
                     fda: pd.DataFrame = None,
                     sector_flow: pd.DataFrame = None,
                     technicals: pd.DataFrame = None,
                     short_int: pd.DataFrame = None,
                     put_call: pd.DataFrame = None,
                     iv_surface: pd.DataFrame = None,
                     # ---- v20 new factor frames -----------------------
                     cot: pd.DataFrame = None,
                     thirteen_f: pd.DataFrame = None,
                     vix_term: pd.DataFrame = None,
                     eia: pd.DataFrame = None,
                     wasde: pd.DataFrame = None,
                     buybacks: pd.DataFrame = None,
                     gtrends: pd.DataFrame = None,
                     form_144: pd.DataFrame = None,
                     whisper: pd.DataFrame = None,
                     hyperliquid: pd.DataFrame = None,
                     twitter: pd.DataFrame = None,
                     r_options: pd.DataFrame = None,
                     yield_curve: pd.DataFrame = None,
                     credit_spread: pd.DataFrame = None,
                     cluster_buys: pd.DataFrame = None) -> pd.DataFrame:
    """Common merge logic — used by both options and shares tracks."""
    df = contracts.copy() if contracts is not None and not contracts.empty else pd.DataFrame()
    if not df.empty and not mp_summary.empty:
        df = df.merge(mp_summary[["ticker", "iv_rank", "skew_25d", "term_slope"]],
                      on="ticker", how="left")
    if not df.empty and sentiment is not None and not sentiment.empty:
        df = df.merge(sentiment[["ticker", "mentions", "sentiment_now",
                                 "sentiment_delta", "velocity"]],
                      on="ticker", how="left")
    elif not df.empty:
        for c in ["mentions", "sentiment_now", "sentiment_delta", "velocity"]:
            df[c] = 0.0
    if not df.empty and fundamentals is not None and not fundamentals.empty:
        df = df.merge(fundamentals[["ticker", "fund_score", "classification",
                                    "earnings_date", "rev_growth", "op_margin", "pe",
                                    "market_cap"]],
                      on="ticker", how="left")
    elif not df.empty:
        for c in ["fund_score", "classification", "earnings_date",
                  "rev_growth", "op_margin", "pe", "market_cap"]:
            df[c] = None
    if not df.empty and insider is not None and not insider.empty:
        df = df.merge(insider[["ticker", "insider_score", "n_buys", "n_sells",
                               "buys_value", "sells_value"]],
                      on="ticker", how="left")
    elif not df.empty:
        for c in ["insider_score", "n_buys", "n_sells", "buys_value", "sells_value"]:
            df[c] = 0.0
    # News
    if not df.empty and news is not None and not news.empty:
        df = df.merge(news[["ticker", "n_24h", "n_7d", "news_sent_24h",
                            "news_sent_7d", "news_delta", "news_velocity",
                            "top_headline"]], on="ticker", how="left")
    elif not df.empty:
        for c in ["n_24h", "n_7d", "news_sent_24h", "news_sent_7d",
                  "news_delta", "news_velocity"]:
            df[c] = 0.0
        df["top_headline"] = ""
    # Earnings
    if not df.empty and earnings is not None and not earnings.empty:
        df = df.merge(earnings[["ticker", "next_earnings_date",
                                "days_to_earnings", "last_eps_surprise_pct",
                                "earnings_score"]], on="ticker", how="left")
    elif not df.empty:
        for c in ["next_earnings_date", "days_to_earnings",
                  "last_eps_surprise_pct", "earnings_score"]:
            df[c] = None

    # Congress
    if not df.empty and congress is not None and not congress.empty:
        df = df.merge(congress[["ticker", "congress_score", "congress_buys_n",
                                "congress_sells_n", "congress_buys_dollar",
                                "congress_n_reps", "congress_n_sens",
                                "congress_top_buyer"]],
                      on="ticker", how="left")
    elif not df.empty:
        for c in ["congress_score", "congress_buys_n", "congress_sells_n",
                  "congress_buys_dollar", "congress_n_reps", "congress_n_sens"]:
            df[c] = 0
        df["congress_top_buyer"] = ""

    # Social (StockTwits + Trump)
    if not df.empty and social is not None and not social.empty:
        df = df.merge(social[["ticker", "social_score", "stocktwits_n",
                              "stocktwits_avg_sent", "stocktwits_n_bull",
                              "stocktwits_n_bear", "trump_n", "trump_avg_sent",
                              "trump_excerpt"]],
                      on="ticker", how="left")
    elif not df.empty:
        for c in ["social_score", "stocktwits_n", "stocktwits_avg_sent",
                  "stocktwits_n_bull", "stocktwits_n_bear", "trump_n",
                  "trump_avg_sent"]:
            df[c] = 0
        df["trump_excerpt"] = ""

    # Analyst (Finnhub recommendations)
    if not df.empty and analyst is not None and not analyst.empty:
        df = df.merge(analyst[["ticker", "analyst_score", "analyst_strong_buy",
                                "analyst_buy", "analyst_hold", "analyst_sell",
                                "analyst_strong_sell", "analyst_total",
                                "analyst_avg", "analyst_momentum"]],
                      on="ticker", how="left")
    elif not df.empty:
        for c in ["analyst_score", "analyst_strong_buy", "analyst_buy",
                  "analyst_hold", "analyst_sell", "analyst_strong_sell",
                  "analyst_total", "analyst_avg", "analyst_momentum"]:
            df[c] = 0

    # UOA (Unusual Options Activity)
    if not df.empty and uoa is not None and not uoa.empty:
        df = df.merge(uoa[["ticker", "uoa_score", "uoa_max_ratio",
                            "uoa_call_ratio", "uoa_put_ratio"]],
                      on="ticker", how="left")
    elif not df.empty:
        for c in ["uoa_score", "uoa_max_ratio", "uoa_call_ratio", "uoa_put_ratio"]:
            df[c] = 0.0

    # Sector relative strength
    if not df.empty and sector_rs is not None and not sector_rs.empty:
        df = df.merge(sector_rs[["ticker", "sector_etf", "ticker_ret_20d",
                                  "sector_ret_20d", "sector_rs_score"]],
                      on="ticker", how="left")
    elif not df.empty:
        for c in ["sector_etf", "ticker_ret_20d", "sector_ret_20d", "sector_rs_score"]:
            df[c] = 0.0 if c != "sector_etf" else None

    # Dark pool (FINRA short-vol)
    if not df.empty and dark_pool is not None and not dark_pool.empty:
        df = df.merge(dark_pool[["ticker", "short_vol_ratio", "dark_pool_score"]],
                      on="ticker", how="left")
    elif not df.empty:
        df["short_vol_ratio"] = 0.0
        df["dark_pool_score"] = 0.0

    # FDA catalyst
    if not df.empty and fda is not None and not fda.empty:
        df = df.merge(fda[["ticker", "next_catalyst_date", "days_to_catalyst",
                            "catalyst_type", "fda_score"]], on="ticker", how="left")
    elif not df.empty:
        for c in ["next_catalyst_date", "days_to_catalyst", "catalyst_type"]:
            df[c] = None
        df["fda_score"] = 0.0

    # Sector ETF flow (joined via SECTOR_MAP)
    if not df.empty and sector_flow is not None and not sector_flow.empty:
        df = df.merge(sector_flow[["ticker", "sector_flow_score"]],
                      on="ticker", how="left", suffixes=("", "_etfflow"))
    elif not df.empty:
        df["sector_flow_score"] = 0.0

    # Technicals (RSI / MACD / Bollinger / MA / 52w distance / ATR / ADX / Stochastic / OBV)
    if not df.empty and technicals is not None and not technicals.empty:
        tech_cols = ["ticker", "tech_score", "rsi", "macd_hist", "bb_percent_b",
                     "ma_cross", "dist_52w_high", "dist_52w_low", "adx",
                     "stoch_k", "obv_slope"]
        keep = [c for c in tech_cols if c in technicals.columns]
        df = df.merge(technicals[keep], on="ticker", how="left", suffixes=("", "_tech"))
    elif not df.empty:
        for c in ["tech_score", "rsi", "macd_hist", "bb_percent_b", "ma_cross",
                  "dist_52w_high", "dist_52w_low", "adx", "stoch_k", "obv_slope"]:
            df[c] = 0.0

    # Short interest (squeeze potential)
    if not df.empty and short_int is not None and not short_int.empty:
        si_cols = ["ticker", "short_int_score", "short_pct_of_float",
                   "short_ratio_days_to_cover", "short_int_change_pct"]
        keep = [c for c in si_cols if c in short_int.columns]
        df = df.merge(short_int[keep], on="ticker", how="left", suffixes=("", "_si"))
    elif not df.empty:
        for c in ["short_int_score", "short_pct_of_float",
                  "short_ratio_days_to_cover", "short_int_change_pct"]:
            df[c] = 0.0

    # Put/Call ratio (contrarian)
    if not df.empty and put_call is not None and not put_call.empty:
        pc_cols = ["ticker", "pc_vol_ratio", "pc_oi_ratio", "pc_score"]
        keep = [c for c in pc_cols if c in put_call.columns]
        df = df.merge(put_call[keep], on="ticker", how="left", suffixes=("", "_pc"))
    elif not df.empty:
        for c in ["pc_vol_ratio", "pc_oi_ratio", "pc_score"]:
            df[c] = 0.0

    # IV surface anomalies
    if not df.empty and iv_surface is not None and not iv_surface.empty:
        iv_cols = ["ticker", "iv_surface_score", "iv_anomaly_max_z",
                   "iv_anomaly_count", "iv_anomaly_top_strike", "iv_anomaly_top_side"]
        keep = [c for c in iv_cols if c in iv_surface.columns]
        df = df.merge(iv_surface[keep], on="ticker", how="left", suffixes=("", "_ivs"))
    elif not df.empty:
        for c in ["iv_surface_score", "iv_anomaly_max_z", "iv_anomaly_count",
                  "iv_anomaly_top_strike", "iv_anomaly_top_side"]:
            df[c] = 0.0 if c not in ("iv_anomaly_top_side",) else None

    # ---- v20 new factor merges ----------------------------------------
    # All v20 factors follow {ticker, <score_name>, ...meta} pattern.
    df = _safe_merge_score(df, cot,          "cot_score",
                            ["cot_market", "cot_net_change", "cot_report_date"])
    df = _safe_merge_score(df, thirteen_f,   "thirteen_f_score",
                            ["tf_n_new", "tf_n_growing", "tf_n_cutting",
                             "tf_n_exiting", "tf_funds"])
    df = _safe_merge_score(df, vix_term,     "vix_term_score",
                            ["vix_regime", "vix_contango_ratio"])
    df = _safe_merge_score(df, eia,          "eia_score",
                            ["eia_meta", "eia_commodity"])
    df = _safe_merge_score(df, wasde,        "wasde_score",
                            ["wasde_proximity", "wasde_days_since"])
    df = _safe_merge_score(df, buybacks,     "buyback_score",
                            ["buyback_date_latest", "buyback_n_filings"])
    df = _safe_merge_score(df, gtrends,      "gtrends_score", ["gtrends_term"])
    df = _safe_merge_score(df, form_144,     "form_144_score",
                            ["form_144_count_30d", "form_144_latest_date"])
    df = _safe_merge_score(df, whisper,      "whisper_score",
                            ["whisper_eps", "whisper_consensus",
                             "whisper_gap_pct", "whisper_report_date"])
    df = _safe_merge_score(df, hyperliquid,  "hyperliquid_score",
                            ["hl_crypto", "hl_funding_annual"])
    df = _safe_merge_score(df, twitter,      "twitter_score",
                            ["twitter_n", "twitter_excerpt"])
    df = _safe_merge_score(df, r_options,    "r_options_score",
                            ["r_options_n", "r_options_avg_sent"])
    df = _safe_merge_score(df, yield_curve,  "curve_score", ["curve_factor"])
    df = _safe_merge_score(df, credit_spread,"credit_score",
                            ["credit_hy_oas", "credit_spread_chg_5d"])
    df = _safe_merge_score(df, cluster_buys, "cluster_buys_score",
                            ["cluster_n_buyers", "cluster_buys_dollar"])

    if not df.empty:
        df["macro_tilt"] = macro.get("macro_tilt", 0.0)
        df["regime"] = macro.get("regime", "neutral")

    return df


# -------- Options track -----------------------------------------------
def fuse_options(contracts: pd.DataFrame, mp_summary: pd.DataFrame,
                 sentiment: pd.DataFrame, fundamentals: pd.DataFrame,
                 insider: pd.DataFrame, macro: Dict[str, Any],
                 news: pd.DataFrame = None, earnings: pd.DataFrame = None,
                 value: pd.DataFrame = None,
                 congress: pd.DataFrame = None,
                 social: pd.DataFrame = None,
                 analyst: pd.DataFrame = None,
                 uoa: pd.DataFrame = None,
                 sector_rs: pd.DataFrame = None,
                 dark_pool: pd.DataFrame = None,
                 fda: pd.DataFrame = None,
                 sector_flow: pd.DataFrame = None,
                 technicals: pd.DataFrame = None,
                 short_int: pd.DataFrame = None,
                 put_call: pd.DataFrame = None,
                 iv_surface: pd.DataFrame = None,
                 # ---- v20 new factor frames -----------------------
                 cot: pd.DataFrame = None,
                 thirteen_f: pd.DataFrame = None,
                 vix_term: pd.DataFrame = None,
                 eia: pd.DataFrame = None,
                 wasde: pd.DataFrame = None,
                 buybacks: pd.DataFrame = None,
                 gtrends: pd.DataFrame = None,
                 form_144: pd.DataFrame = None,
                 whisper: pd.DataFrame = None,
                 hyperliquid: pd.DataFrame = None,
                 twitter: pd.DataFrame = None,
                 r_options: pd.DataFrame = None,
                 yield_curve: pd.DataFrame = None,
                 credit_spread: pd.DataFrame = None,
                 cluster_buys: pd.DataFrame = None) -> pd.DataFrame:
    """Long-only option ranking. Returns one DataFrame with both calls and puts."""
    if contracts is None or contracts.empty:
        return pd.DataFrame()

    df = _join_per_ticker(contracts, mp_summary, sentiment, fundamentals, insider, macro,
                          news=news, earnings=earnings, congress=congress, social=social,
                          analyst=analyst, uoa=uoa, sector_rs=sector_rs,
                          dark_pool=dark_pool, fda=fda, sector_flow=sector_flow,
                          technicals=technicals, short_int=short_int,
                          put_call=put_call, iv_surface=iv_surface,
                          cot=cot, thirteen_f=thirteen_f, vix_term=vix_term,
                          eia=eia, wasde=wasde, buybacks=buybacks,
                          gtrends=gtrends, form_144=form_144, whisper=whisper,
                          hyperliquid=hyperliquid, twitter=twitter,
                          r_options=r_options, yield_curve=yield_curve,
                          credit_spread=credit_spread, cluster_buys=cluster_buys)
    # v20.1 — defragment after long merge chain (silences PerformanceWarning)
    df = df.copy()
    # Merge value
    if value is not None and not value.empty:
        df = df.merge(value[["ticker", "value_score", "value_bucket", "earnings_yield",
                             "fcf_yield", "graham_score"]], on="ticker", how="left")
    else:
        for c in ["value_score", "value_bucket", "earnings_yield",
                  "fcf_yield", "graham_score"]:
            df[c] = 0.0 if c == "value_score" else None

    # v20.1 — defragment again after the value merge
    df = df.copy()

    # ---- Signal extraction ------------------------------------------
    df["vol_premium"] = df["iv_market"] - df["fair_vol"]
    # Mispricing: positive => undervalued => good to buy (we want this aligned)
    df["mispricing_signal"] = -df["vol_premium"]
    # IV rank: low IV-rank favours BUYING premium, high disfavours
    df["iv_rank_signal"] = -((df["iv_rank"].fillna(50) - 50) / 50)

    # Side multiplier — secondary signals are bullish-tilted; align to side
    side_mult = np.where(df["side"] == "call", 1.0, -1.0)

    df["sentiment_aligned"] = df["sentiment_delta"].fillna(0) * side_mult
    df["fund_aligned"] = df["fund_score"].fillna(0) * side_mult
    df["insider_aligned"] = df["insider_score"].fillna(0) * side_mult
    df["macro_aligned"] = df["macro_tilt"].fillna(0) * side_mult
    # Skew: high put skew → puts richer / calls relatively cheap → boosts calls
    df["skew_aligned"] = df["skew_25d"].fillna(0) * side_mult
    # News Δsentiment, side-aligned (positive news → bullish → boosts calls)
    df["news_aligned"] = df["news_delta"].fillna(0) * side_mult
    # Earnings catalyst, side-aligned (positive surprise + imminent → boost call/dampen put)
    df["earnings_aligned"] = df["earnings_score"].fillna(0) * side_mult
    # Value, side-aligned (cheap stock → bullish → boost call ideas)
    df["value_aligned"] = df.get("value_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Congress, side-aligned (Congressional buying → bullish → boost call ideas)
    df["congress_aligned"] = df.get("congress_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Social, side-aligned (positive Trump/StockTwits → bullish → boost calls)
    df["social_aligned"] = df.get("social_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Analyst, side-aligned (analyst momentum bullish → bullish → boost calls)
    df["analyst_aligned"] = df.get("analyst_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # UOA: positive uoa_score = call-side flow heavy → boost calls
    df["uoa_aligned"] = df.get("uoa_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Sector RS: stock outperforming sector → bullish bias
    df["sector_rs_aligned"] = df.get("sector_rs_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Dark pool: high short-vol-ratio → bearish (score is already signed: negative=bearish)
    df["dark_pool_aligned"] = df.get("dark_pool_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # FDA catalyst: high score = imminent catalyst — always boosts BOTH directions (vol coming)
    # We bias toward calls since catalysts more often pop than crash; keep neutral if you prefer.
    df["fda_aligned"] = df.get("fda_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Sector flow: hot sector → bullish bias on its constituents
    df["sector_flow_aligned"] = df.get("sector_flow_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Technicals: composite directional score (low fusion weight — context, not main driver)
    df["tech_aligned"] = df.get("tech_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Short interest: high SI → squeeze potential → bullish bias
    df["short_int_aligned"] = df.get("short_int_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Put/Call ratio: contrarian (positive pc_score = bullish at extremes)
    df["put_call_aligned"] = df.get("pc_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # IV surface: anomaly on call strikes = bullish demand; on puts = bearish hedge demand
    df["iv_surface_aligned"] = df.get("iv_surface_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult

    # ---- v20 new factor side-alignment --------------------------------
    # CoT: managed-money net buying = bullish for the ETF/equity bucket
    df["cot_aligned"] = df.get("cot_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # 13F: smart-money net adds = bullish
    df["thirteen_f_aligned"] = df.get("thirteen_f_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # VIX term: contango = bullish equity, backwardation = bearish
    df["vix_term_aligned"] = df.get("vix_term_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # EIA: inventory draw = bullish energy equities
    df["eia_aligned"] = df.get("eia_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # WASDE: drift direction already encoded in score
    df["wasde_aligned"] = df.get("wasde_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Buybacks: announcement = bullish
    df["buybacks_aligned"] = df.get("buyback_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Google Trends: rising interest = bullish (retail catching on)
    df["gtrends_aligned"] = df.get("gtrends_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Form 144: pre-sale notices = bearish (score already negative)
    df["form_144_aligned"] = df.get("form_144_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Whisper: high bar / low bar already encoded
    df["whisper_aligned"] = df.get("whisper_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Hyperliquid: positive funding (longs paying) = bullish crypto-corr equities
    df["hyperliquid_aligned"] = df.get("hyperliquid_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Twitter cashtag sentiment, side-aligned
    df["twitter_aligned"] = df.get("twitter_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # r/options sticky sentiment, side-aligned
    df["r_options_aligned"] = df.get("r_options_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Yield curve: signed score (banks benefit from rising rates + steepening)
    df["yield_curve_aligned"] = df.get("curve_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Credit spread: stress = bearish (score already negative when widening)
    df["credit_spread_aligned"] = df.get("credit_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult
    # Cluster buys: high-edge insider subset, side-aligned
    df["cluster_buys_aligned"] = df.get("cluster_buys_score", pd.Series(0.0, index=df.index)).fillna(0) * side_mult

    # v20.1 — defragment after the long aligned-column assignment block
    df = df.copy()

    # Post-earnings news boost: when news is fresh AFTER an earnings print,
    # multiply news_aligned by 1.5x to capture the "transcript echo" effect.
    if "post_earnings_window" in df.columns:
        boost_mask = df["post_earnings_window"].fillna(False).astype(bool)
        df.loc[boost_mask, "news_aligned"] = df.loc[boost_mask, "news_aligned"] * 1.5

    # Cross-sectional z-scores
    df["z_mispricing"] = zscore(winsor(df["mispricing_signal"]))
    df["z_iv_rank"] = zscore(df["iv_rank_signal"])
    df["z_skew"] = zscore(df["skew_aligned"])
    df["z_sent"] = zscore(df["sentiment_aligned"])
    df["z_fund"] = zscore(df["fund_aligned"])
    df["z_insider"] = zscore(df["insider_aligned"])
    df["z_macro"] = zscore(df["macro_aligned"])
    df["z_news"] = zscore(df["news_aligned"])
    df["z_earnings"] = zscore(df["earnings_aligned"])
    df["z_value"] = zscore(df["value_aligned"])
    df["z_congress"] = zscore(df["congress_aligned"])
    df["z_social"] = zscore(df["social_aligned"])
    df["z_analyst"] = zscore(df["analyst_aligned"])
    df["z_uoa"] = zscore(df["uoa_aligned"])
    df["z_sector_rs"] = zscore(df["sector_rs_aligned"])
    df["z_dark_pool"] = zscore(df["dark_pool_aligned"])
    df["z_fda"] = zscore(df["fda_aligned"])
    df["z_sector_flow"] = zscore(df["sector_flow_aligned"])
    df["z_tech"] = zscore(df["tech_aligned"])
    df["z_short_int"] = zscore(df["short_int_aligned"])
    df["z_put_call"] = zscore(df["put_call_aligned"])
    df["z_iv_surface"] = zscore(df["iv_surface_aligned"])

    # ---- v20 new z-scores ---------------------------------------------
    df["z_cot"]          = zscore(df["cot_aligned"])
    df["z_thirteen_f"]   = zscore(df["thirteen_f_aligned"])
    df["z_vix_term"]     = zscore(df["vix_term_aligned"])
    df["z_eia"]          = zscore(df["eia_aligned"])
    df["z_wasde"]        = zscore(df["wasde_aligned"])
    df["z_buybacks"]     = zscore(df["buybacks_aligned"])
    df["z_gtrends"]      = zscore(df["gtrends_aligned"])
    df["z_form_144"]     = zscore(df["form_144_aligned"])
    df["z_whisper"]      = zscore(df["whisper_aligned"])
    df["z_hyperliquid"]  = zscore(df["hyperliquid_aligned"])
    df["z_twitter"]      = zscore(df["twitter_aligned"])
    df["z_r_options"]    = zscore(df["r_options_aligned"])
    df["z_yield_curve"]  = zscore(df["yield_curve_aligned"])
    df["z_credit_spread"]= zscore(df["credit_spread_aligned"])
    df["z_cluster_buys"] = zscore(df["cluster_buys_aligned"])

    # v20.1 — defragment one more time before the heavy fusion sum
    df = df.copy()

    # Weighted fusion → directional buy score
    w = SIGNAL_WEIGHTS
    df["fused_score"] = (
        w["mispricing"]    * df["z_mispricing"]
        + w["iv_rank"]     * df["z_iv_rank"]
        + w["skew"]        * df["z_skew"]
        + w["sentiment_d"] * df["z_sent"]
        + w["fundamentals"]* df["z_fund"]
        + w["insider"]     * df["z_insider"]
        + w["macro"]       * df["z_macro"]
        + w.get("news", 0) * df["z_news"]
        + w.get("earnings", 0) * df["z_earnings"]
        + w.get("value", 0) * df["z_value"]
        + w.get("congress", 0) * df["z_congress"]
        + w.get("social", 0) * df["z_social"]
        + w.get("analyst", 0) * df["z_analyst"]
        + w.get("uoa", 0) * df["z_uoa"]
        + w.get("sector_rs", 0) * df["z_sector_rs"]
        + w.get("dark_pool", 0) * df["z_dark_pool"]
        + w.get("fda", 0) * df["z_fda"]
        + w.get("sector_flow", 0) * df["z_sector_flow"]
        + w.get("technicals", 0) * df["z_tech"]
        + w.get("short_int", 0) * df["z_short_int"]
        + w.get("put_call", 0) * df["z_put_call"]
        + w.get("iv_surface", 0) * df["z_iv_surface"]
        # ---- v20 NEW FACTORS ----
        + w.get("cot", 0)          * df["z_cot"]
        + w.get("thirteen_f", 0)   * df["z_thirteen_f"]
        + w.get("vix_term", 0)     * df["z_vix_term"]
        + w.get("eia", 0)          * df["z_eia"]
        + w.get("wasde", 0)        * df["z_wasde"]
        + w.get("buybacks", 0)     * df["z_buybacks"]
        + w.get("gtrends", 0)      * df["z_gtrends"]
        + w.get("form_144", 0)     * df["z_form_144"]
        + w.get("whisper", 0)      * df["z_whisper"]
        + w.get("hyperliquid", 0)  * df["z_hyperliquid"]
        + w.get("twitter", 0)      * df["z_twitter"]
        + w.get("r_options", 0)    * df["z_r_options"]
        + w.get("yield_curve", 0)  * df["z_yield_curve"]
        + w.get("credit_spread", 0)* df["z_credit_spread"]
        + w.get("cluster_buys", 0) * df["z_cluster_buys"]
    )

    # Long-only: drop everything with negative buy score
    df = df[df["fused_score"] > 0].copy()
    if df.empty:
        return df

    df["confidence"] = df["fused_score"].apply(_confidence)
    df["signal"] = df.apply(lambda r: _option_signal_label(r["side"], r["vol_premium"]), axis=1)
    df["reasoning"] = df.apply(_option_reasoning, axis=1)
    df["risks"] = df.apply(_option_risks, axis=1)
    df["contract"] = df.apply(
        lambda r: f"{r['ticker']} {r['expiry']} {'C' if r['side']=='call' else 'P'} {r['strike']:g}",
        axis=1,
    )
    df["rank_score"] = df["fused_score"]
    df = df.sort_values("rank_score", ascending=False).reset_index(drop=True)
    return df


def _sector_of(ticker: str) -> str:
    """Quick sector lookup for diversification guard. Returns 'OTHER' if unknown."""
    try:
        from engines.sector_rs import SECTOR_MAP
        return SECTOR_MAP.get(ticker, "OTHER")
    except Exception:
        return "OTHER"


def top_options(df: pd.DataFrame, max_calls: int = TOP_N_CALLS,
                max_puts: int = TOP_N_PUTS,
                max_per_sector: int = 3) -> pd.DataFrame:
    """Pick top calls and top puts with diversity rules:
      - 1 per ticker max
      - At most `max_per_sector` picks per same sector (prevents 5 tech megacaps).
    """
    if df.empty:
        return df
    seen_tickers = set()
    sector_counts: Dict[str, int] = {}
    calls, puts = [], []
    for _, r in df.iterrows():
        t = r["ticker"]
        if t in seen_tickers:
            continue
        sec = _sector_of(t)
        # Correlation guard: cap picks per sector
        if sector_counts.get(sec, 0) >= max_per_sector:
            continue
        if r["side"] == "call" and len(calls) < max_calls:
            calls.append(r)
            seen_tickers.add(t)
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
        elif r["side"] == "put" and len(puts) < max_puts:
            puts.append(r)
            seen_tickers.add(t)
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
        if len(calls) >= max_calls and len(puts) >= max_puts:
            break
    out = pd.DataFrame(calls + puts).reset_index(drop=True)
    return out


# -------- Shares track ------------------------------------------------
def fuse_shares(small_cap_universe: List[str], sentiment: pd.DataFrame,
                fundamentals: pd.DataFrame, insider: pd.DataFrame,
                macro: Dict[str, Any], excluded_tickers: set = None,
                news: pd.DataFrame = None, earnings: pd.DataFrame = None,
                value: pd.DataFrame = None,
                congress: pd.DataFrame = None) -> pd.DataFrame:
    """Score small caps for long shares.

    Built from the multi-factor stack only (no option pricing). Bullish-aligned
    only — small caps where Δsentiment, fundamentals, insiders, and macro all
    line up positive.
    """
    excluded_tickers = excluded_tickers or set()
    seen = set()
    rows = []
    for t in small_cap_universe:
        if t in excluded_tickers or t in seen:
            continue
        seen.add(t)
        rows.append({"ticker": t})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)

    if sentiment is not None and not sentiment.empty:
        df = df.merge(sentiment[["ticker", "mentions", "sentiment_now",
                                 "sentiment_delta", "velocity"]],
                      on="ticker", how="left")
    else:
        for c in ["mentions", "sentiment_now", "sentiment_delta", "velocity"]:
            df[c] = 0.0
    if fundamentals is not None and not fundamentals.empty:
        df = df.merge(fundamentals[["ticker", "fund_score", "classification",
                                    "earnings_date", "rev_growth", "op_margin", "pe",
                                    "market_cap"]],
                      on="ticker", how="left")
    else:
        for c in ["fund_score", "classification", "earnings_date",
                  "rev_growth", "op_margin", "pe", "market_cap"]:
            df[c] = None
    if insider is not None and not insider.empty:
        df = df.merge(insider[["ticker", "insider_score", "n_buys", "n_sells",
                               "buys_value", "sells_value"]],
                      on="ticker", how="left")
    else:
        for c in ["insider_score", "n_buys", "n_sells", "buys_value", "sells_value"]:
            df[c] = 0.0

    # Merge news + earnings if available
    if news is not None and not news.empty:
        df = df.merge(news[["ticker", "news_delta", "news_velocity", "top_headline",
                            "n_24h"]], on="ticker", how="left")
    else:
        df["news_delta"] = 0.0
        df["news_velocity"] = 0.0
        df["top_headline"] = ""
        df["n_24h"] = 0
    if earnings is not None and not earnings.empty:
        df = df.merge(earnings[["ticker", "next_earnings_date", "days_to_earnings",
                                "earnings_score"]], on="ticker", how="left")
    else:
        df["next_earnings_date"] = None
        df["days_to_earnings"] = None
        df["earnings_score"] = 0.0

    if value is not None and not value.empty:
        df = df.merge(value[["ticker", "value_score", "value_bucket"]], on="ticker", how="left")
    else:
        df["value_score"] = 0.0
        df["value_bucket"] = None

    if congress is not None and not congress.empty:
        df = df.merge(congress[["ticker", "congress_score", "congress_buys_n",
                                "congress_n_reps", "congress_n_sens", "congress_top_buyer"]],
                      on="ticker", how="left")
    else:
        df["congress_score"] = 0.0
        df["congress_buys_n"] = 0
        df["congress_n_reps"] = 0
        df["congress_n_sens"] = 0
        df["congress_top_buyer"] = ""

    df["macro_tilt"] = macro.get("macro_tilt", 0.0)
    df["regime"] = macro.get("regime", "neutral")

    # Cross-sectional z-scores (no side multiplier — shares are inherently long bullish)
    df["z_sent"] = zscore(df["sentiment_delta"].fillna(0))
    df["z_fund"] = zscore(df["fund_score"].fillna(0))
    df["z_insider"] = zscore(df["insider_score"].fillna(0))
    df["z_velocity"] = zscore(df["velocity"].fillna(0))
    df["z_news"] = zscore(df["news_delta"].fillna(0))
    df["z_earnings"] = zscore(df["earnings_score"].fillna(0))
    df["z_value"] = zscore(df["value_score"].fillna(0))
    df["z_congress"] = zscore(df["congress_score"].fillna(0))

    # Bullish-tilt fusion (shares-only)
    df["share_score"] = (
        0.16 * df["z_sent"]
        + 0.14 * df["z_fund"]
        + 0.14 * df["z_insider"]
        + 0.05 * df["z_velocity"]
        + 0.06 * df["macro_tilt"]
        + 0.09 * df["z_news"]
        + 0.09 * df["z_earnings"]
        + 0.15 * df["z_value"]
        + 0.12 * df["z_congress"]
    )

    # Long-only: keep bullish-aligned above threshold
    df = df[df["share_score"] >= SHARES_MIN_SCORE].copy()
    if df.empty:
        return df

    df["confidence"] = df["share_score"].apply(lambda s: int(min(100, 50 + s * 22)))
    df["signal"] = "Long Shares (small cap)"
    df["reasoning"] = df.apply(_share_reasoning, axis=1)
    df["risks"] = df.apply(_share_risks, axis=1)
    df["rank_score"] = df["share_score"]
    df = df.sort_values("rank_score", ascending=False).reset_index(drop=True)
    return df


def top_shares(df: pd.DataFrame, n: int = TOP_N_SHARES) -> pd.DataFrame:
    if df.empty:
        return df
    return df.head(n).reset_index(drop=True)


# -------- TradingView watchlist export -------------------------------
def to_tv_watchlist(calls: pd.DataFrame, puts: pd.DataFrame, shares: pd.DataFrame) -> str:
    """Emit a TradingView-compatible watchlist file.

    Format: one symbol per line, with section headers. Importable via TV's
    watchlist 'Import file' on the right-hand panel.
    """
    lines = ["###Optedge Long Calls"]
    if calls is not None and not calls.empty:
        for _, r in calls.iterrows():
            lines.append(f"NASDAQ:{r['ticker']}")
    lines.append("###Optedge Long Puts")
    if puts is not None and not puts.empty:
        for _, r in puts.iterrows():
            lines.append(f"NASDAQ:{r['ticker']}")
    lines.append("###Optedge Long Shares (small cap)")
    if shares is not None and not shares.empty:
        for _, r in shares.iterrows():
            lines.append(f"NASDAQ:{r['ticker']}")
    return "\n".join(lines) + "\n"

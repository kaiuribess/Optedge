"""Technical indicators engine — pure-pandas, no external deps.

Computes per ticker:
  - RSI(14)
  - MACD (12, 26, 9): line, signal, histogram
  - Bollinger Bands (20, 2): %B (position within bands), bandwidth
  - MA50 / MA200 + golden/death cross flag
  - Distance from 52-week high / low
  - ATR(14)
  - ADX(14) — trend strength
  - Stochastic(14, 3) — %K, %D
  - On-Balance Volume slope (last 20d)

Combines into a single directional `tech_score` in roughly [-1, +1].
This is wired into fusion as a SMALL weight (0.03) — context, not driver.

The full per-indicator values are exposed for display on cards as context
chips/tooltips, so you can see RSI/MACD/52w% without trusting them as
the primary rank input.
"""
from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

import numpy as np
import pandas as pd

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.technicals")


# -------- Indicator math (pure pandas, no TA lib) --------------------
def _rsi(close: pd.Series, period: int = 14) -> Optional[float]:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    down = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / down.replace(0, 1e-9)
    rsi = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1])


def _macd(close: pd.Series) -> Dict[str, Optional[float]]:
    if len(close) < 27:
        return {"macd": None, "signal": None, "hist": None}
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    sig = macd_line.ewm(span=9, adjust=False).mean()
    hist = macd_line - sig
    return {
        "macd": float(macd_line.iloc[-1]),
        "signal": float(sig.iloc[-1]),
        "hist": float(hist.iloc[-1]),
    }


def _bollinger(close: pd.Series, period: int = 20, k: float = 2.0) -> Dict[str, Optional[float]]:
    if len(close) < period:
        return {"bb_percent_b": None, "bb_bandwidth": None}
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = sma + k * std
    lower = sma - k * std
    cur_price = close.iloc[-1]
    cur_u = upper.iloc[-1]
    cur_l = lower.iloc[-1]
    cur_m = sma.iloc[-1]
    if cur_u - cur_l <= 0:
        return {"bb_percent_b": 0.5, "bb_bandwidth": 0}
    return {
        "bb_percent_b": float((cur_price - cur_l) / (cur_u - cur_l)),
        "bb_bandwidth": float((cur_u - cur_l) / max(cur_m, 1e-9)),
    }


def _moving_averages(close: pd.Series) -> Dict[str, Optional[float]]:
    if len(close) < 50:
        return {"ma50": None, "ma200": None, "ma_cross": None}
    ma50 = close.rolling(50).mean().iloc[-1]
    ma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else None
    cross = None
    if ma200 is not None:
        # Golden cross = 50 above 200; Death cross = 50 below 200
        cross = 1 if ma50 > ma200 else -1
    return {"ma50": float(ma50), "ma200": float(ma200) if ma200 else None, "ma_cross": cross}


def _distance_52w(close: pd.Series) -> Dict[str, Optional[float]]:
    if len(close) < 5:
        return {"dist_52w_high": None, "dist_52w_low": None}
    window = close.iloc[-252:] if len(close) > 252 else close
    hi = window.max()
    lo = window.min()
    cur = close.iloc[-1]
    return {
        "dist_52w_high": float((cur - hi) / hi) if hi > 0 else None,    # negative = below high
        "dist_52w_low": float((cur - lo) / lo) if lo > 0 else None,     # positive = above low
    }


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> Optional[float]:
    if len(close) < period + 1:
        return None
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr) if not pd.isna(atr) else None


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> Optional[float]:
    """Simplified ADX. Returns 0-100; higher = stronger trend regardless of direction."""
    if len(close) < period * 2 + 1:
        return None
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=low.index)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * plus_dm.rolling(period).mean() / atr.replace(0, 1e-9)
    minus_di = 100 * minus_dm.rolling(period).mean() / atr.replace(0, 1e-9)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    adx = dx.rolling(period).mean().iloc[-1]
    return float(adx) if not pd.isna(adx) else None


def _stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
                period: int = 14, smooth: int = 3) -> Dict[str, Optional[float]]:
    if len(close) < period + smooth:
        return {"stoch_k": None, "stoch_d": None}
    ll = low.rolling(period).min()
    hh = high.rolling(period).max()
    k_raw = 100 * (close - ll) / (hh - ll).replace(0, 1e-9)
    k = k_raw.rolling(smooth).mean()
    d = k.rolling(smooth).mean()
    return {"stoch_k": float(k.iloc[-1]), "stoch_d": float(d.iloc[-1])}


def _obv_slope(close: pd.Series, volume: pd.Series, window: int = 20) -> Optional[float]:
    """Slope of On-Balance Volume over the last `window` days, normalized."""
    if len(close) < window + 1 or volume is None:
        return None
    direction = np.sign(close.diff().fillna(0))
    obv = (direction * volume).cumsum()
    recent = obv.iloc[-window:]
    if len(recent) < 2:
        return None
    # Normalize: slope per day / mean OBV magnitude
    x = np.arange(len(recent))
    slope = np.polyfit(x, recent.values, 1)[0]
    magnitude = max(abs(recent.mean()), 1)
    return float(slope / magnitude)


def _compose_tech_score(ind: Dict[str, Any]) -> float:
    """Combine indicators into a single directional score in roughly [-1, +1].

    Designed so each component contributes a small piece; no single indicator
    dominates. Bullish signals positive, bearish negative.
    """
    score = 0.0
    # RSI: oversold (<30) bullish, overbought (>70) bearish — but mean-reverting
    rsi = ind.get("rsi")
    if rsi is not None:
        if rsi < 30:
            score += 0.3
        elif rsi > 70:
            score -= 0.3
        else:
            # Sloped: 50 = neutral, +0.1 per 10 above 50, etc.
            score += (rsi - 50) / 200  # max ±0.1
    # MACD histogram: positive bullish, negative bearish
    hist = ind.get("macd_hist")
    if hist is not None:
        score += max(-0.2, min(0.2, hist / 5))   # scale; clipped
    # Bollinger %B
    pb = ind.get("bb_percent_b")
    if pb is not None:
        if pb < 0.05:
            score += 0.25     # below lower band = oversold bounce candidate
        elif pb > 0.95:
            score -= 0.25     # above upper band = reversal candidate
    # MA cross
    cross = ind.get("ma_cross")
    if cross == 1:
        score += 0.15
    elif cross == -1:
        score -= 0.15
    # Distance from 52w high: too far above 52w-low is overextended
    dlow = ind.get("dist_52w_low")
    if dlow is not None and dlow > 1.0:
        score -= 0.1     # 2x off the low → mean reversion risk
    dhigh = ind.get("dist_52w_high")
    if dhigh is not None and dhigh < -0.30:
        score += 0.1     # 30%+ off the high → potential value setup
    # ADX trend strength: amplifies the direction
    adx = ind.get("adx")
    if adx is not None and adx > 25 and cross is not None:
        score += 0.10 * cross
    # Stochastic
    k = ind.get("stoch_k")
    if k is not None:
        if k < 20:
            score += 0.10
        elif k > 80:
            score -= 0.10
    # OBV slope (volume confirmation)
    obv = ind.get("obv_slope")
    if obv is not None:
        score += max(-0.1, min(0.1, obv * 50))
    # Clamp
    return max(-1.0, min(1.0, score))


def _process_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    h = data_provider.get_history(ticker, period="1y")
    if h is None or h.empty or len(h) < 30:
        return None
    close = h["Close"].astype(float)
    high = h["High"].astype(float) if "High" in h.columns else close
    low = h["Low"].astype(float) if "Low" in h.columns else close
    vol = h["Volume"].astype(float) if "Volume" in h.columns else None

    rsi = _rsi(close)
    macd = _macd(close)
    bb = _bollinger(close)
    ma = _moving_averages(close)
    d52 = _distance_52w(close)
    atr = _atr(high, low, close)
    adx = _adx(high, low, close)
    stoch = _stochastic(high, low, close)
    obv_slope = _obv_slope(close, vol) if vol is not None else None

    indicators = {
        "rsi": rsi,
        "macd": macd["macd"], "macd_signal": macd["signal"], "macd_hist": macd["hist"],
        "bb_percent_b": bb["bb_percent_b"], "bb_bandwidth": bb["bb_bandwidth"],
        "ma50": ma["ma50"], "ma200": ma["ma200"], "ma_cross": ma["ma_cross"],
        "dist_52w_high": d52["dist_52w_high"], "dist_52w_low": d52["dist_52w_low"],
        "atr": atr, "adx": adx,
        "stoch_k": stoch["stoch_k"], "stoch_d": stoch["stoch_d"],
        "obv_slope": obv_slope,
    }
    indicators["tech_score"] = round(_compose_tech_score(indicators), 3)
    indicators["ticker"] = ticker
    return indicators


def run(universe: List[str], max_workers: int = 8) -> pd.DataFrame:
    """Compute technical indicators for each ticker."""
    universe = list(dict.fromkeys(universe))
    log.info("technicals: %d tickers (parallel, %d workers)", len(universe), max_workers)
    rows = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_process_ticker, t): t for t in universe}
        for fut in as_completed(futures):
            try:
                r = fut.result()
                if r:
                    rows.append(r)
            except Exception as e:
                log.debug("technicals fail: %s", e)
            completed += 1
            if completed % 100 == 0 or completed == len(universe):
                log.info("[technicals %d/%d]", completed, len(universe))
    df = pd.DataFrame(rows)
    if not df.empty:
        bullish = (df["tech_score"] > 0.3).sum()
        bearish = (df["tech_score"] < -0.3).sum()
        log.info("technicals done: %d tickers, %d bullish, %d bearish",
                 len(df), bullish, bearish)
    return df

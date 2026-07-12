"""Render the Optedge quant cockpit as a self-contained HTML file.

Layout:
  - Header + run stats banner
  - Macro regime panel (VIX, yields, regime)
  - Long Calls / Long Puts / Long Shares card grids
  - News flow panel (top recently-newsy names)
  - Earnings calendar (next 14d)
  - Insider activity heatmap (top buyers / top sellers)
  - WSB trending heatmap
  - Ranked tables (full snapshot)
  - TradingView watchlist export
  - Methodology appendix
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import html
import logging
import math

import pandas as pd

from optedge.strategy_profile import (
    DISCOVERY_PROFILE,
    STRATEGY_VERSION,
    SWING_EXECUTION_PROFILE,
)

try:
    from fusion.attribution import attribution_chip as _attrib_chip
except Exception:
    def _attrib_chip(row, top_k=3):
        return ""

log = logging.getLogger("optedge.dashboard")
ROOT = Path(__file__).resolve().parent.parent


def _safe_int(v, default: int = 0) -> int:
    """Convert to int, treating None/NaN/inf as the default. Avoids the
    common 'cannot convert float NaN to integer' crash when DataFrame fields
    are NaN."""
    try:
        if v is None:
            return default
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return default
        return int(v)
    except (ValueError, TypeError):
        return default


# -------- Tiny formatters ---------------------------------------------
def _fmt_pct(x, digits=1):
    if x is None or pd.isna(x):
        return "-"
    return f"{x*100:+.{digits}f}%"


def _fmt_num(x, digits=2):
    if x is None or pd.isna(x):
        return "-"
    return f"{x:.{digits}f}"


def _fmt_money(x):
    if x is None or pd.isna(x) or x == 0:
        return "-"
    if x >= 1e9:
        return f"${x/1e9:.2f}B"
    if x >= 1e6:
        return f"${x/1e6:.1f}M"
    if x >= 1e3:
        return f"${x/1e3:.0f}K"
    return f"${x:.0f}"


def _fmt_text(x, default: str = "-") -> str:
    if x is None or pd.isna(x):
        return default
    text = str(x).strip()
    return text if text and text.lower() != "nan" else default


def _badge(label: str, color: str) -> str:
    return f'<span class="badge" style="background:{color}">{label}</span>'


def _confidence_bar(conf: int) -> str:
    width = max(0, min(100, conf))
    color = "#10b981" if conf >= 70 else "#f59e0b" if conf >= 55 else "#94a3b8"
    return (f'<div class="conf-bar"><div class="conf-fill" '
            f'style="width:{width}%;background:{color}"></div>'
            f'<span class="conf-text">{conf}</span></div>')


def _trade_status_chip(row: pd.Series) -> str:
    status = row.get("trade_status") or "Watch"
    colors = {
        "Trade": ("#10b981", "Ready"),
        "Watch": ("#f59e0b", "Watch"),
        "Skip": ("#ef4444", "Skip"),
    }
    color, label = colors.get(status, ("#94a3b8", html.escape(str(status))))
    score = row.get("trade_score")
    title = ""
    if score is not None and not pd.isna(score):
        title = f' title="Trade score {float(score):.2f}"'
    return (f'<span class="chip trade-status {html.escape(str(status).lower())}"{title} '
            f'style="background:{color}22;color:{color};border-color:{color}55">'
            f'{label}</span>')


def _quote_quality_chip(row: pd.Series) -> str:
    source = str(row.get("chain_source") or row.get("quote_source") or "unknown").strip()
    quality = str(row.get("quote_quality") or "").strip().lower()
    if not source or source.lower() == "nan":
        source = "unknown"
    src_label = source.replace("_", " ").title()
    if source.lower() == "tradier" or quality in {"live_or_broker", "live", "broker"}:
        color = "#10b981"
        label = f"Live {src_label}"
        title = "Broker/live option chain source; still verify spreads before manual execution."
    elif source.lower().startswith("cboe"):
        color = "#f59e0b"
        label = "CBOE delayed"
        title = "Free CBOE delayed option chain source."
    elif source.lower().startswith("nasdaq"):
        color = "#f59e0b"
        label = "NASDAQ free"
        title = "Free NASDAQ option chain source; treat as non-live unless verified."
    elif source.lower().startswith("yahoo_options"):
        color = "#94a3b8"
        label = "Yahoo options"
        title = "Bounded Yahoo options fallback; free/delayed research data."
    elif source.lower().startswith("yfinance"):
        color = "#94a3b8"
        label = "Yahoo fallback"
        title = "Free yfinance fallback; can be delayed, partial, or rate-limited."
    else:
        color = "#94a3b8"
        label = src_label
        title = "Quote source quality is unknown; verify before manual execution."
    return (
        f'<span class="chip quote-source" title="{html.escape(title)}" '
        f'style="background:{color}20;color:{color};border:1px solid {color}55">'
        f'{html.escape(label)}</span>'
    )


def _position_identity(row: Dict[str, Any]) -> tuple:
    """Stable identity for lifecycle rows that may not have a position_id."""
    pid = row.get("position_id")
    if pid:
        return ("id", str(pid))
    return (
        str(row.get("asset") or ""),
        str(row.get("ticker") or row.get("symbol") or ""),
        str(row.get("side") or row.get("direction") or ""),
        str(row.get("strike") or row.get("contract") or ""),
        str(row.get("expiry") or ""),
        str(row.get("entry_time") or ""),
        str(row.get("entry_price") or ""),
    )


def _dedupe_position_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _position_identity(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _is_win_pnl(value: Any) -> bool:
    try:
        return float(value) > 0
    except Exception:
        return False


def _exit_bucket(row: pd.Series) -> str:
    reason = str(row.get("exit_reason") or row.get("outcome") or "").strip().lower()
    if not reason or reason == "nan":
        return "win" if _is_win_pnl(row.get("pnl_pct")) else "loss"
    if reason in {"target", "hard_target"}:
        return "target"
    if reason in {"stop", "hard_stop"}:
        return "stop"
    return reason


def _open_position_label(row: Dict[str, Any]) -> str:
    ticker = str(row.get("ticker") or row.get("symbol") or "-").upper()
    side = str(row.get("side") or row.get("direction") or "").upper()
    strike = row.get("strike")
    expiry = str(row.get("expiry") or "")
    contract = str(row.get("contract") or "")
    if strike not in (None, "", "-") and expiry:
        try:
            strike_txt = f"{float(strike):g}"
        except Exception:
            strike_txt = str(strike)
        return f"{ticker} {side[:1]} {strike_txt} {expiry[-5:]}"
    if contract:
        return f"{ticker} {side} {contract}".strip()
    return f"{ticker} {side}".strip()


# -------- Cards --------------------------------------------------------
def _option_card(row: pd.Series) -> str:
    side_color = "#10b981" if row["side"] == "call" else "#f87171"
    side_label = "LONG CALL" if row["side"] == "call" else "LONG PUT"
    moneyness = (row["strike"] / row["spot"] - 1) * 100
    _h = row.get("top_headline")
    headline = _h.strip() if isinstance(_h, str) else ""
    earnings = row.get("days_to_earnings")
    earnings_html = ""
    if earnings is not None and not pd.isna(earnings) and 0 <= earnings <= 60:
        earnings_html = f'<span class="chip earn">EPS EPS in {int(earnings)}d</span>'
    headline_html = ""
    if headline:
        headline_html = f'<div class="headline">News {html.escape(headline[:90])}</div>'
    # Predicted return chip
    pred_html = ""
    pred_opt = row.get("pred_option_return_pct")
    if pred_opt is not None and not pd.isna(pred_opt) and abs(pred_opt) > 0.005:
        color = "#10b981" if pred_opt > 0 else "#ef4444"
        pred_html = (f'<span class="chip" style="background:{color}20;color:{color};font-weight:600">'
                     f'pred {pred_opt*100:+.1f}%</span>')
    # EV chip
    ev_html = ""
    ev_pct = row.get("ev_pct")
    if ev_pct is not None and not pd.isna(ev_pct):
        ev_color = "#10b981" if ev_pct > 0 else "#ef4444"
        ev_html = (f'<span class="chip" style="background:{ev_color}20;color:{ev_color}">'
                   f'EV {ev_pct*100:+.0f}%</span>')
    buyer_edge_html = ""
    buyer_edge = row.get("buyer_edge_pct")
    if buyer_edge is not None and not pd.isna(buyer_edge):
        edge_color = "#10b981" if buyer_edge >= 0 else "#ef4444"
        buyer_edge_html = (
            f'<span class="chip" style="background:{edge_color}20;color:{edge_color}" '
            f'title="Model value advantage for a long buyer after round-trip spread">'
            f'buyer edge {buyer_edge*100:+.1f}%</span>'
        )
    # Congress chip
    congress_html = ""
    cong_score = row.get("congress_score") or 0
    if cong_score and not pd.isna(cong_score) and abs(cong_score) > 0.3:
        c = "#10b981" if cong_score > 0 else "#ef4444"
        n_buys = _safe_int(row.get("congress_buys_n"))
        congress_html = (f'<span class="chip" style="background:{c}20;color:{c}">'
                         f' Cong {cong_score:+.1f} ({n_buys}P)</span>')
    # Analyst chip
    analyst_html = ""
    analyst_score = row.get("analyst_score") or 0
    if analyst_score and not pd.isna(analyst_score) and abs(analyst_score) > 0.5:
        ac = "#10b981" if analyst_score > 0 else "#ef4444"
        sb = _safe_int(row.get("analyst_strong_buy"))
        bs = _safe_int(row.get("analyst_buy"))
        ss = _safe_int(row.get("analyst_strong_sell")) + _safe_int(row.get("analyst_sell"))
        analyst_html = (f'<span class="chip" style="background:{ac}20;color:{ac}">'
                        f'Analyst {sb}SB/{bs}B/{ss}S ({analyst_score:+.1f})</span>')
    # Technicals - context chip (RSI + MACD direction + 52w distance)
    tech_html = ""
    rsi = row.get("rsi")
    if rsi is not None and not pd.isna(rsi):
        macd_h = row.get("macd_hist") or 0
        dist_hi = row.get("dist_52w_high") or 0
        rsi_color = "#ef4444" if rsi > 70 else "#10b981" if rsi < 30 else "#94a3b8"
        macd_glyph = "^" if macd_h > 0 else "v"
        tech_html = (f'<span class="chip" title="RSI {rsi:.0f}  -  MACD hist {macd_h:+.2f} '
                     f' -  52w high {dist_hi*100:+.0f}%" '
                     f'style="background:{rsi_color}20;color:{rsi_color}">'
                     f'Trend RSI {rsi:.0f} MACD{macd_glyph}</span>')
    # Short interest chip (squeeze setup awareness)
    short_int_html = ""
    spof = row.get("short_pct_of_float")
    if spof is not None and not pd.isna(spof) and spof > 0.10:
        si_color = "#f59e0b" if spof > 0.20 else "#3b82f6"
        chg = row.get("short_int_change_pct") or 0
        chg_glyph = "^" if chg > 0.05 else "v" if chg < -0.05 else "->"
        short_int_html = (f'<span class="chip" title="Short % of float; change vs prior month: {chg*100:+.1f}%" '
                          f'style="background:{si_color}20;color:{si_color}">'
                          f'SI SI {spof*100:.0f}% {chg_glyph}</span>')
    # Put/Call ratio chip (contrarian)
    pc_html = ""
    pc_vol = row.get("pc_vol_ratio")
    if pc_vol is not None and not pd.isna(pc_vol) and (pc_vol > 1.5 or pc_vol < 0.5):
        # extreme P/C
        contrarian = "bullish" if pc_vol > 1.5 else "bearish"
        pc_color = "#10b981" if pc_vol > 1.5 else "#ef4444"
        pc_html = (f'<span class="chip" title="Crowd positioning - extreme is contrarian" '
                   f'style="background:{pc_color}20;color:{pc_color}">'
                   f'P/C P/C {pc_vol:.2f}</span>')
    # IV surface anomaly chip
    iv_anom_html = ""
    iv_z = row.get("iv_anomaly_max_z")
    if iv_z is not None and not pd.isna(iv_z) and abs(iv_z) > 2.0:
        ivc = "#a78bfa"
        top_strike = row.get("iv_anomaly_top_strike")
        top_side = row.get("iv_anomaly_top_side") or ""
        title = f"IV anomaly: {top_side} {top_strike:g} is {iv_z:+.1f}sigma off the smile" if top_strike else ""
        iv_anom_html = (f'<span class="chip" title="{title}" '
                        f'style="background:{ivc}20;color:{ivc}">'
                        f'IV IV {iv_z:+.1f}sigma</span>')
    # Kelly + sizing
    kelly_pct = row.get("kelly_pct") or 0
    sized_dollars = row.get("actual_dollars") or 0
    sized_contracts = _safe_int(row.get("suggested_contracts"))
    trade_status = str(row.get("trade_status") or "").strip().lower()
    actionable_size = trade_status == "trade" and sized_contracts > 0
    kelly_html = ""
    if kelly_pct and not pd.isna(kelly_pct) and kelly_pct > 0:
        kelly_html = (f'<span class="chip" style="background:#3b82f620;color:#3b82f6">'
                      f'1/4Kelly {kelly_pct*100:.1f}%</span>')
    # Exit triggers (option-specific)
    stop_p = row.get("stop_price")
    target_p = row.get("target_price")
    exit_block = ""
    if stop_p is not None and target_p is not None and not pd.isna(stop_p) and stop_p > 0:
        exit_block = f"""
  <div class="exit-block">
    <h4>Exit triggers</h4>
    <div class="exit-row">
      <span class="exit-stop">Cong Stop @ ${stop_p:.2f} <span class="muted">(-50%)</span></span>
      <span class="exit-target">Target Target @ ${target_p:.2f} <span class="muted">(+100%)</span></span>
    </div>
  </div>"""

    sizing_block = ""
    if actionable_size:
        sizing_block = f"""
  <div class="sizing-block">
    <h4>Position size <span class="muted">(Kelly)</span></h4>
    <div class="sizing-row">
      <span><strong>{sized_contracts}</strong> contract{'s' if sized_contracts != 1 else ''}</span>
      <span class="muted">~= ${sized_dollars:,.0f}</span>
      <span class="muted">{kelly_pct*100:.1f}% of bankroll</span>
    </div>
  </div>"""
    else:
        gate_reason = str(row.get("trade_gate_reason") or "not_actionable").replace("_", " ")
        sizing_block = """<div class="sizing-block warn">
    <h4>Position size</h4>
    <p>Not executable: %s.</p>
  </div>""" % html.escape(gate_reason)

    return f"""
<article class="card" data-ticker="{html.escape(row["ticker"]).upper()}" data-side="{row["side"]}" data-status="{html.escape(str(row.get('trade_status') or 'Watch')).lower()}" data-conf="{_safe_int(row.get("confidence"))}" data-pred="{(pred_opt or 0)*100:.2f}" data-ev="{(ev_pct or 0)*100:.2f}" data-kelly="{(kelly_pct or 0)*100:.2f}" data-dte="{_safe_int(row.get('dte'))}">
  <header class="card-head">
    <div class="ticker-block">
      <span class="ticker">{html.escape(row["ticker"])}</span>
      {_badge(side_label, side_color)}
      {_trade_status_chip(row)}
      {earnings_html}
      {pred_html}
      {ev_html}
      {buyer_edge_html}
      {kelly_html}
      {_quote_quality_chip(row)}
      {congress_html}
      {analyst_html}
      {tech_html}
      {short_int_html}
      {pc_html}
      {iv_anom_html}
    </div>
    {_confidence_bar(_safe_int(row.get("confidence")))}
  </header>
  <div class="contract">
    <span class="contract-line">{html.escape(row["contract"])}</span>
    <span class="muted">@ ${_fmt_num(row['mid'])}  -  spot ${_fmt_num(row['spot'])}  -  {moneyness:+.1f}%  -  {_safe_int(row.get('dte'))}d</span>
  </div>
  <div class="grid">
    <div><span class="lab">IV</span><span class="val">{_fmt_pct(row['iv_market'])}</span></div>
    <div><span class="lab">HV30</span><span class="val">{_fmt_pct(row['fair_vol'])}</span></div>
    <div><span class="lab">Vol prem</span><span class="val">{_fmt_pct(row['vol_premium'])}</span></div>
    <div><span class="lab">Delta</span><span class="val">{_fmt_num(row['delta'])}</span></div>
    <div><span class="lab">OI</span><span class="val">{_safe_int(row.get('open_interest')):,}</span></div>
    <div><span class="lab">Spread</span><span class="val">{_fmt_pct(row['spread_pct'])}</span></div>
    <div><span class="lab">Buyer edge</span><span class="val">{_fmt_pct(row.get('buyer_edge_pct'))}</span></div>
    <div><span class="lab">Pricing</span><span class="val">{html.escape(str(row.get('pricing_direction') or '-')).replace('_', ' ')}</span></div>
  </div>
  {headline_html}
  {sizing_block}
  {exit_block}
  <div class="reason">
    <h4>Why</h4>
    <p>{html.escape(row.get("reasoning",""))}</p>
  </div>
  <div class="attribution muted" style="font-size:11px;margin-top:6px;font-family:monospace;">
    Target drivers: {html.escape(_attrib_chip(row))}
  </div>
  <div class="risks">
    <h4>Risks</h4>
    <p>{html.escape(row.get("risks",""))}</p>
  </div>
</article>
"""


def _value_card(row: pd.Series) -> str:
    """Card for a "good value" play."""
    bucket = row.get("value_bucket") or "-"
    bucket_color = {"deep value": "#10b981", "value": "#3b82f6",
                    "fair": "#94a3b8", "expensive": "#ef4444"}.get(bucket, "#94a3b8")
    pe = row.get("pe")
    pe_str = f"{pe:.1f}" if pe is not None and not pd.isna(pe) and pe > 0 else "-"
    fcf_y = row.get("fcf_yield")
    ey = row.get("earnings_yield")
    insider = row.get("insider_score") or 0
    _h = row.get("top_headline")
    headline = _h.strip() if isinstance(_h, str) else ""
    headline_html = (f'<div class="headline">News {html.escape(headline[:90])}</div>'
                     if headline else "")
    insider_chip = ""
    if insider and not pd.isna(insider) and abs(insider) > 0.5:
        ins_color = "#10b981" if insider > 0 else "#ef4444"
        n_buys = _safe_int(row.get("n_buys"))
        n_sells = _safe_int(row.get("n_sells"))
        insider_chip = (f'<span class="chip" style="background:{ins_color}20;color:{ins_color}">'
                        f'Insider {insider:+.1f} ({n_buys}P/{n_sells}S)</span>')
    return f"""
<article class="card" data-ticker="{html.escape(str(row['ticker'])).upper()}" data-side="value" data-status="watch" data-conf="0" data-pred="0" data-ev="0" data-kelly="0">
  <header class="card-head">
    <div class="ticker-block">
      <span class="ticker">{html.escape(row["ticker"])}</span>
      {_badge("VALUE", bucket_color)}
      <span class="chip" style="background:{bucket_color}20;color:{bucket_color}">{html.escape(bucket)}</span>
    </div>
    <div class="muted" style="font-family:'JetBrains Mono', monospace; font-size:13px;">
      score <strong>{row['value_score']:+.2f}</strong>
    </div>
  </header>
  <div class="grid">
    <div><span class="lab">P/E</span><span class="val">{pe_str}</span></div>
    <div><span class="lab">FCF yld</span><span class="val">{_fmt_pct(fcf_y) if fcf_y is not None else '-'}</span></div>
    <div><span class="lab">EY</span><span class="val">{_fmt_pct(ey) if ey is not None else '-'}</span></div>
    <div><span class="lab">EV/EBITDA</span><span class="val">{_fmt_num(row.get('ev_ebitda'),1)}</span></div>
    <div><span class="lab">Op margin</span><span class="val">{_fmt_pct(row.get('roic_proxy')) if row.get('roic_proxy') is not None else '-'}</span></div>
    <div><span class="lab">Graham</span><span class="val">{_safe_int(row.get('graham_score'))}/6</span></div>
  </div>
  <div style="margin-top:8px; display:flex; gap:6px; flex-wrap:wrap;">
    {insider_chip}
  </div>
  {headline_html}
</article>
"""


def _futures_card(row: pd.Series) -> str:
    is_long = (row.get("futures_score") or 0) > 0
    side_color = "#10b981" if is_long else "#f87171"
    side_label = "LONG" if is_long else "SHORT"
    proxy = _fmt_text(row.get("etf"))
    contract = _fmt_text(row.get("contract"))
    micro = "micro" if row.get("using_micro") else "full"
    context = row.get("futures_context_score")
    rank_score = row.get("rank_score")
    rank_html = f" - rank {float(rank_score):+.2f}" if rank_score is not None and not pd.isna(rank_score) else ""
    atr = row.get("atr20") if row.get("atr20") is not None else row.get("atr_estimate")
    trade_status = row.get("trade_status") or "Watch"
    return f"""
<article class="card" data-ticker="{html.escape(str(row['symbol'])).upper()}" data-side="futures" data-status="{html.escape(str(trade_status)).lower()}" data-conf="0" data-pred="{float(row.get('futures_score') or 0) * 100:.2f}" data-ev="0" data-kelly="{float(row.get('kelly_pct') or 0) * 100:.2f}">
  <header class="card-head">
    <div class="ticker-block">
      <span class="ticker">{html.escape(row["symbol"])}</span>
      {_badge(side_label, side_color)}
      <span class="chip">{html.escape(_fmt_text(row.get('kind'), ''))}</span>
      <span class="chip">{html.escape(str(trade_status))}</span>
    </div>
    <div class="muted" style="font-family:'JetBrains Mono', monospace; font-size:13px;">
      score <strong>{row['futures_score']:+.2f}</strong>{rank_html}
    </div>
  </header>
  <div class="contract">
    <span class="contract-line">{html.escape(_fmt_text(row.get('name'), ''))}</span>
    <span class="muted">spot ${_fmt_num(row.get('spot'))} · ETF proxy {html.escape(proxy)} · {html.escape(str(contract))} ({micro})</span>
  </div>
  <div class="grid">
    <div><span class="lab">5d</span><span class="val">{_fmt_pct(row.get('ret_5d')) if row.get('ret_5d') is not None else '-'}</span></div>
    <div><span class="lab">20d</span><span class="val">{_fmt_pct(row.get('ret_20d')) if row.get('ret_20d') is not None else '-'}</span></div>
    <div><span class="lab">60d</span><span class="val">{_fmt_pct(row.get('ret_60d')) if row.get('ret_60d') is not None else '-'}</span></div>
    <div><span class="lab">HV20</span><span class="val">{_fmt_pct(row.get('hv20')) if row.get('hv20') is not None else '-'}</span></div>
    <div><span class="lab">ATR20</span><span class="val">{_fmt_num(atr, 2) if atr is not None else '-'}</span></div>
    <div><span class="lab">52w pos</span><span class="val">{_fmt_num((row.get('range_pos') or 0)*100, 0)}%</span></div>
    <div><span class="lab">Context</span><span class="val">{_fmt_num(context, 2) if context is not None else '-'}</span></div>
    <div><span class="lab">Contracts</span><span class="val">{_safe_int(row.get('suggested_contracts'))}</span></div>
    <div><span class="lab">Stop</span><span class="val">{_fmt_num(row.get('stop_price'), 2)}</span></div>
    <div><span class="lab">Target</span><span class="val">{_fmt_num(row.get('target_price'), 2)}</span></div>
    <div><span class="lab">Risk</span><span class="val">${_fmt_num(row.get('suggested_dollars_risk'), 0)}</span></div>
    <div><span class="lab">R:R</span><span class="val">{_fmt_num(row.get('reward_risk_ratio'), 2)}</span></div>
  </div>
</article>
"""


def _share_card(row: pd.Series) -> str:
    cls = row.get("classification") or "-"
    mcap = row.get("market_cap")
    mcap_str = _fmt_money(mcap)
    _h = row.get("top_headline")
    headline = _h.strip() if isinstance(_h, str) else ""
    earnings = row.get("days_to_earnings")
    earnings_html = ""
    if earnings is not None and not pd.isna(earnings) and 0 <= earnings <= 60:
        earnings_html = f'<span class="chip earn">EPS EPS in {int(earnings)}d</span>'
    headline_html = ""
    if headline:
        headline_html = f'<div class="headline">News {html.escape(headline[:90])}</div>'
    pred_html = ""
    pred_stk = row.get("pred_stock_return_pct")
    if pred_stk is not None and not pd.isna(pred_stk) and abs(pred_stk) > 0.002:
        color = "#10b981" if pred_stk > 0 else "#ef4444"
        pred_html = (f'<span class="chip" style="background:{color}20;color:{color};font-weight:600">'
                     f'pred {pred_stk*100:+.1f}%</span>')
    ev_pct = row.get("ev_pct")
    ev_html = ""
    if ev_pct is not None and not pd.isna(ev_pct) and abs(ev_pct) > 0.002:
        c = "#10b981" if ev_pct > 0 else "#ef4444"
        ev_html = f'<span class="chip" style="background:{c}20;color:{c}">EV {ev_pct*100:+.1f}%</span>'
    kelly_pct = row.get("kelly_pct") or 0
    sized_dollars = row.get("suggested_dollars") or 0
    kelly_html = ""
    if kelly_pct and not pd.isna(kelly_pct) and kelly_pct > 0:
        kelly_html = (f'<span class="chip" style="background:#3b82f620;color:#3b82f6">'
                      f'1/4Kelly {kelly_pct*100:.1f}%</span>')
    # Exit triggers for shares
    stop_pct_v = row.get("stop_pct")
    target_pct_v = row.get("target_pct")
    exit_block = ""
    if stop_pct_v is not None and target_pct_v is not None and not pd.isna(stop_pct_v):
        exit_block = f"""
  <div class="exit-block">
    <h4>Exit triggers</h4>
    <div class="exit-row">
      <span class="exit-stop">Cong Stop @ {stop_pct_v*100:+.0f}%</span>
      <span class="exit-target">Target Target @ {target_pct_v*100:+.0f}%</span>
    </div>
  </div>"""

    sizing_block = ""
    if kelly_pct and kelly_pct > 0 and sized_dollars > 0:
        sizing_block = f"""
  <div class="sizing-block">
    <h4>Position size <span class="muted">(Kelly)</span></h4>
    <div class="sizing-row">
      <span><strong>${sized_dollars:,.0f}</strong></span>
      <span class="muted">{kelly_pct*100:.1f}% of bankroll</span>
    </div>
  </div>"""

    return f"""
<article class="card" data-ticker="{html.escape(row["ticker"]).upper()}" data-side="shares" data-status="{html.escape(str(row.get('trade_status') or 'Watch')).lower()}" data-conf="{_safe_int(row.get("confidence"))}" data-pred="{(pred_stk or 0)*100:.2f}" data-ev="{(ev_pct or 0)*100:.2f}" data-kelly="{(kelly_pct or 0)*100:.2f}">
  <header class="card-head">
    <div class="ticker-block">
      <span class="ticker">{html.escape(row["ticker"])}</span>
      {_badge("LONG SHARES", "#3b82f6")}
      {_trade_status_chip(row)}
      {earnings_html}
      {pred_html}
      {ev_html}
      {kelly_html}
    </div>
    {_confidence_bar(_safe_int(row.get("confidence")))}
  </header>
  <div class="contract">
    <span class="contract-line">{html.escape(cls.upper())}  -  mcap {mcap_str}</span>
    <span class="muted">share score {row['share_score']:+.2f}</span>
  </div>
  <div class="grid">
    <div><span class="lab">Delta Sent</span><span class="val">{_fmt_num(row.get('sentiment_delta'),2)}</span></div>
    <div><span class="lab">Mentions</span><span class="val">{_safe_int(row.get('mentions'))}</span></div>
    <div><span class="lab">Fund</span><span class="val">{_fmt_num(row.get('fund_score'),2)}</span></div>
    <div><span class="lab">Insider</span><span class="val">{_fmt_num(row.get('insider_score'),2)}</span></div>
    <div><span class="lab">News 24h</span><span class="val">{_safe_int(row.get('n_24h'))}</span></div>
    <div><span class="lab">Velocity</span><span class="val">{_safe_int(row.get('velocity')):+d}</span></div>
  </div>
  {headline_html}
  {sizing_block}
  {exit_block}
  <div class="reason">
    <h4>Why</h4>
    <p>{html.escape(row.get("reasoning",""))}</p>
  </div>
  <div class="risks">
    <h4>Risks</h4>
    <p>{html.escape(row.get("risks",""))}</p>
  </div>
</article>
"""


# -------- Macro / stats banner ----------------------------------------
def _macro_banner(macro: Dict[str, Any]) -> str:
    regime = macro.get("regime", "neutral")
    tilt = macro.get("macro_tilt", 0.0)
    color = {"risk_on": "#10b981", "risk_off": "#ef4444"}.get(regime, "#94a3b8")
    vix = macro.get("vix")
    slope = macro.get("yield_curve_slope")
    spy3m = macro.get("spy_3m_return")
    cpi_yoy = macro.get("cpi_yoy")
    unrate = macro.get("unrate")
    hy_spread = macro.get("hy_spread")
    fed_funds = macro.get("fed_funds")
    initial_claims = macro.get("initial_claims")
    t10y3m = macro.get("t10y3m")

    # Second row only renders if FRED data is available
    fred_row = ""
    if any(v is not None for v in (cpi_yoy, unrate, hy_spread, fed_funds, t10y3m)):
        cpi_color = "#ef4444" if (cpi_yoy or 0) > 0.04 else "#10b981" if (cpi_yoy or 0) < 0.025 else "#94a3b8"
        hy_color = "#ef4444" if (hy_spread or 0) > 5 else "#10b981" if (hy_spread or 0) < 3 else "#94a3b8"
        curve_color = "#ef4444" if (t10y3m or 0) < 0 else "#10b981"
        fred_row = f"""
  <div class="macro-grid" style="margin-top:10px;">
    <div><span class="lab">CPI YoY</span><span class="val" style="color:{cpi_color}">{_fmt_pct(cpi_yoy) if cpi_yoy is not None else '-'}</span></div>
    <div><span class="lab">Unemp</span><span class="val">{_fmt_num(unrate, 1) if unrate is not None else '-'}%</span></div>
    <div><span class="lab">Fed Funds</span><span class="val">{_fmt_num(fed_funds, 2) if fed_funds is not None else '-'}%</span></div>
    <div><span class="lab">HY spread</span><span class="val" style="color:{hy_color}">{_fmt_num(hy_spread, 2) if hy_spread is not None else '-'}%</span></div>
    <div><span class="lab">10Y-3M</span><span class="val" style="color:{curve_color}">{_fmt_num(t10y3m, 2) if t10y3m is not None else '-'}%</span></div>
  </div>"""

    return f"""
<section class="macro" style="border-left-color:{color}">
  <div class="macro-head">
    <span class="regime-dot" style="background:{color}"></span>
    <h2>Macro: <strong>{regime.replace('_',' ').upper()}</strong></h2>
    <span class="muted">tilt {tilt:+.2f}</span>
  </div>
  <div class="macro-grid">
    <div><span class="lab">VIX</span><span class="val">{_fmt_num(vix, 1) if vix else '-'}</span></div>
    <div><span class="lab">10Y</span><span class="val">{_fmt_num(macro.get('yield_10y'), 2) if macro.get('yield_10y') else '-'}%</span></div>
    <div><span class="lab">3M</span><span class="val">{_fmt_num(macro.get('yield_3m'), 2) if macro.get('yield_3m') else '-'}%</span></div>
    <div><span class="lab">10Y-3M</span><span class="val">{_fmt_num(slope, 2) if slope is not None else '-'}%</span></div>
    <div><span class="lab">SPY 3m</span><span class="val">{_fmt_pct(spy3m) if spy3m is not None else '-'}</span></div>
  </div>{fred_row}
</section>
"""


def _stats_panel(elapsed: float, universe_size: int, n_calls: int, n_puts: int,
                 n_shares: int, n_news: int, n_earnings: int,
                 trending_count: int) -> str:
    return f"""
<section class="stats-panel">
  <div class="stat"><span class="stat-val">{elapsed:.1f}s</span><span class="stat-lab">runtime</span></div>
  <div class="stat"><span class="stat-val">{universe_size}</span><span class="stat-lab">universe</span></div>
  <div class="stat"><span class="stat-val">{n_calls}</span><span class="stat-lab">calls</span></div>
  <div class="stat"><span class="stat-val">{n_puts}</span><span class="stat-lab">puts</span></div>
  <div class="stat"><span class="stat-val">{n_shares}</span><span class="stat-lab">shares</span></div>
  <div class="stat"><span class="stat-val">{n_news}</span><span class="stat-lab">news rows</span></div>
  <div class="stat"><span class="stat-val">{n_earnings}</span><span class="stat-lab">earnings</span></div>
  <div class="stat"><span class="stat-val">{trending_count}</span><span class="stat-lab">WSB trending</span></div>
</section>
"""


# -------- Heatmaps & panels -------------------------------------------
def _news_flow_panel(news_df: pd.DataFrame, top_n: int = 10) -> str:
    if news_df is None or news_df.empty:
        return ""
    df = news_df.copy()
    # Rank by velocity x |news_delta| + raw count
    df["news_rank"] = df["news_velocity"].abs() + df["news_delta"].abs() * 5 + df["n_24h"] * 0.3
    top = df.sort_values("news_rank", ascending=False).head(top_n)
    rows = []
    for _, r in top.iterrows():
        if not (r.get("n_24h") or 0):
            continue
        sent = r.get("news_delta") or 0
        sent_color = "#10b981" if sent > 0.05 else "#ef4444" if sent < -0.05 else "#94a3b8"
        rows.append(f"""
<div class="news-row">
  <div class="news-tk"><strong>{html.escape(r['ticker'])}</strong></div>
  <div class="news-counts">
    <span class="chip">{int(r['n_24h'])} / 24h</span>
    <span class="chip">{int(r['n_7d'])} / 7d</span>
    <span class="chip" style="background:{sent_color}20;color:{sent_color}">Delta {sent:+.2f}</span>
  </div>
  <div class="news-headline">{html.escape((r.get('top_headline') or '')[:110])}</div>
</div>""")
    if not rows:
        return ""
    return f"""
<section class="panel">
  <h3>News News Flow <span class="muted">(top by velocity x Deltasentiment)</span></h3>
  <div class="news-list">{''.join(rows)}</div>
</section>
"""


def _earnings_calendar(earnings_df: pd.DataFrame, days: int = 14) -> str:
    if earnings_df is None or earnings_df.empty:
        return ""
    df = earnings_df.copy()
    df = df[df["days_to_earnings"].notna() & (df["days_to_earnings"] >= 0) & (df["days_to_earnings"] <= days)]
    if df.empty:
        return ""
    df = df.sort_values("days_to_earnings")
    rows = []
    for _, r in df.iterrows():
        sup = r.get("last_eps_surprise_pct")
        sup_color = "#10b981" if (sup or 0) > 0.02 else "#ef4444" if (sup or 0) < -0.02 else "#94a3b8"
        sup_str = f"{sup*100:+.1f}%" if sup is not None else "-"
        rows.append(f"""
<div class="earn-row">
  <div class="earn-tk"><strong>{html.escape(r['ticker'])}</strong></div>
  <div class="earn-date">{html.escape(r.get('next_earnings_date') or '-')}</div>
  <div class="earn-dte"><span class="chip">in {int(r['days_to_earnings'])}d</span></div>
  <div class="earn-sup" style="color:{sup_color}">last surprise {sup_str}</div>
</div>""")
    return f"""
<section class="panel">
  <h3>EPS Earnings Calendar <span class="muted">(next {days}d)</span></h3>
  <div class="earn-list">{''.join(rows)}</div>
</section>
"""


def _insider_heatmap(insider_df: pd.DataFrame, n_each: int = 8) -> str:
    if insider_df is None or insider_df.empty:
        return ""
    df = insider_df.copy()
    if "insider_score" not in df.columns:
        return ""
    df = df[df["insider_score"].abs() > 0.1].copy()
    if df.empty:
        return ""
    buyers = df.sort_values("insider_score", ascending=False).head(n_each)
    sellers = df.sort_values("insider_score").head(n_each)

    def _row(r, color):
        val = max(r.get("buys_value", 0) or 0, r.get("sells_value", 0) or 0)
        return f"""
<div class="ins-row">
  <div class="ins-tk"><strong>{html.escape(r['ticker'])}</strong></div>
  <div class="ins-score" style="color:{color}">{r['insider_score']:+.2f}</div>
  <div class="ins-counts"><span class="chip">{_safe_int(r.get('n_buys'))}P / {_safe_int(r.get('n_sells'))}S</span></div>
  <div class="ins-val muted">{_fmt_money(val)}</div>
</div>"""
    buyers_html = "".join(_row(r, "#10b981") for _, r in buyers.iterrows()) or "<p class='muted'>No notable buys</p>"
    sellers_html = "".join(_row(r, "#ef4444") for _, r in sellers.iterrows()) or "<p class='muted'>No notable sells</p>"
    return f"""
<section class="panel">
  <h3>Insider Insider Activity <span class="muted">(SEC EDGAR, last 90d)</span></h3>
  <div class="two-col">
    <div>
      <h4 class="sub">Top net buyers</h4>
      <div class="ins-list">{buyers_html}</div>
    </div>
    <div>
      <h4 class="sub">Top net sellers</h4>
      <div class="ins-list">{sellers_html}</div>
    </div>
  </div>
</section>
"""


def _wsb_panel(trending: List[str], sentiment_df: pd.DataFrame,
               trending_meta: List = None) -> str:
    """Render the WSB trending tile grid.

    Pulls mention counts from `trending_meta` if provided (WSB engine output),
    otherwise falls back to the sentiment_df (which only counts the past 48h).
    """
    if not trending:
        return ""
    rows = []
    sent_lookup = {}
    if sentiment_df is not None and not sentiment_df.empty and "ticker" in sentiment_df.columns:
        try:
            sent_lookup = sentiment_df.set_index("ticker").to_dict(orient="index")
        except Exception:
            sent_lookup = {}
    meta_lookup = {m["ticker"]: m for m in (trending_meta or [])}

    for t in trending[:25]:
        meta = meta_lookup.get(t, {})
        # Prefer WSB engine mention count (it's the actual source of trending)
        mentions = int(meta.get("mentions", 0))
        ups = int(meta.get("ups", 0))
        # Deltasentiment from the sentiment engine
        srow = sent_lookup.get(t, {})
        delta = float(srow.get("sentiment_delta", 0) or 0)
        sent_color = "#10b981" if delta > 0.05 else "#ef4444" if delta < -0.05 else "#94a3b8"
        ups_str = f"  -  {ups:,} ups" if ups > 0 else ""
        rows.append(f"""
<div class="wsb-tile">
  <span class="wsb-ticker">{html.escape(t)}</span>
  <span class="chip" style="background:{sent_color}20;color:{sent_color}">Delta {delta:+.2f}</span>
  <span class="muted">{mentions} mention{'s' if mentions != 1 else ''}{ups_str}</span>
</div>""")
    return f"""
<section class="panel">
  <h3>WSB WSB Trending <span class="muted">({len(trending)} discovered live, added to universe)</span></h3>
  <div class="wsb-grid">{''.join(rows)}</div>
</section>
"""


# -------- Tables -------------------------------------------------------
def _options_table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "<p class='muted'>No long-option ideas pass filters.</p>"
    rows = []
    for i, r in df.iterrows():
        side_color = "#10b981" if r["side"] == "call" else "#f87171"
        side_label = "C" if r["side"] == "call" else "P"
        status = html.escape(str(r.get("trade_status") or "Watch"))
        source_html = _quote_quality_chip(r)
        rows.append(f"""
<tr>
  <td>{i+1}</td>
  <td><strong>{html.escape(r['ticker'])}</strong></td>
  <td>{html.escape(r['contract'])}</td>
  <td><span class="dot" style="background:{side_color}"></span>{side_label}</td>
  <td>{status}</td>
  <td>{int(r['confidence'])}</td>
  <td>{_fmt_pct(r.get('ev_pct'))}</td>
  <td>{_fmt_pct(r.get('kelly_pct'))}</td>
  <td>{_fmt_pct(r.get('buyer_edge_pct'))}</td>
  <td>{html.escape(str(r.get('pricing_direction') or '-')).replace('_', ' ')}</td>
  <td>{_fmt_pct(r['iv_market'])}</td>
  <td>{_fmt_pct(r['fair_vol'])}</td>
  <td>{_fmt_pct(r['vol_premium'])}</td>
  <td>{int(r['dte'])}d</td>
  <td>${_fmt_num(r['mid'])}</td>
  <td>{source_html}</td>
</tr>""")
    return f"""
<table class="ranked">
  <thead><tr>
    <th>#</th><th>Ticker</th><th>Contract</th><th>Side</th>
    <th>Status</th><th>Conf</th><th>EV</th><th>Kelly</th><th>Buyer edge</th><th>Pricing</th><th>IV</th><th>HV30</th><th>Vol prem</th><th>DTE</th><th>Mid</th><th>Source</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""


def _shares_table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "<p class='muted'>No share ideas above the score threshold.</p>"
    rows = []
    for i, r in df.iterrows():
        cls = r.get("classification") or "-"
        mcap = r.get("market_cap")
        mcap_str = _fmt_money(mcap)
        status = html.escape(str(r.get("trade_status") or "Watch"))
        rows.append(f"""
<tr>
  <td>{i+1}</td>
  <td><strong>{html.escape(r['ticker'])}</strong></td>
  <td>{html.escape(cls)}</td>
  <td>{mcap_str}</td>
  <td>{status}</td>
  <td>{int(r['confidence'])}</td>
  <td>{_fmt_pct(r.get('ev_pct'))}</td>
  <td>{_fmt_pct(r.get('kelly_pct'))}</td>
  <td>{r['share_score']:+.2f}</td>
  <td>{_fmt_num(r.get('sentiment_delta'), 2)}</td>
  <td>{_safe_int(r.get('mentions'))}</td>
  <td>{_fmt_num(r.get('fund_score'), 1)}</td>
  <td>{_fmt_num(r.get('insider_score'), 1)}</td>
</tr>""")
    return f"""
<table class="ranked">
  <thead><tr>
    <th>#</th><th>Ticker</th><th>Class</th><th>Mcap</th>
    <th>Status</th><th>Conf</th><th>EV</th><th>Kelly</th><th>Score</th><th>DeltaSent</th><th>Mentions</th><th>Fund</th><th>Insider</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""


_CSS = """
:root {
  --bg: #090b10; --panel: #10131a; --panel-2: #151a23;
  --panel-3: #1b2330; --border: #283142; --text: #edf2f7;
  --muted: #98a2b3; --accent: #d7dee8; --focus: #38bdf8;
  --good: #10b981; --warn: #f59e0b; --bad: #ef4444;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0; background: radial-gradient(circle at 50% -20%, #182230 0, var(--bg) 38%);
  color: var(--text);
  font-family: -apple-system, "Inter", "Helvetica Neue", Arial, sans-serif;
  font-size: 14px; line-height: 1.55;
}
.wrap { max-width: 1480px; margin: 0 auto; padding: 32px 28px 96px; }
header.top {
  display: flex; align-items: baseline; justify-content: space-between;
  border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px;
}
header.top h1 { font-size: 28px; letter-spacing: -0.5px; font-weight: 600; margin: 0; }
header.top .meta { color: var(--muted); font-size: 11px; font-family: "JetBrains Mono", monospace; }
.muted { color: var(--muted); }

section.macro {
  background: var(--panel); border: 1px solid var(--border);
  border-left: 4px solid #94a3b8; border-radius: 8px;
  padding: 16px 20px; margin-bottom: 16px;
}
.macro-head { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.macro-head h2 { margin: 0; font-size: 15px; font-weight: 500; }
.regime-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.macro-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; }
.macro-grid .lab, .grid .lab {
  display: block; font-size: 10px; text-transform: uppercase;
  color: var(--muted); letter-spacing: 0.5px; margin-bottom: 2px;
}
.macro-grid .val, .grid .val {
  font-size: 15px; font-family: "JetBrains Mono", monospace;
}

.stats-panel {
  display: grid; grid-template-columns: repeat(8, 1fr); gap: 12px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 16px; margin-bottom: 24px;
}
.stat { display: flex; flex-direction: column; align-items: center; }
.stat-val { font-size: 18px; font-weight: 600; font-family: "JetBrains Mono", monospace; }
.stat-lab { font-size: 9px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.5px; margin-top: 2px; }

h2.section-title {
  font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px;
  color: var(--muted); margin: 32px 0 14px; display: flex; align-items: center; gap: 12px;
}
h2.section-title .count {
  background: var(--panel); padding: 2px 8px; border-radius: 4px;
  font-family: "JetBrains Mono", monospace; color: var(--accent);
}

/* Collapsible dashboard sections */
details.dash-section { margin: 24px 0 0; }
details.dash-section > summary {
  cursor: pointer; user-select: none; list-style: none;
  outline: none;
}
details.dash-section > summary::-webkit-details-marker { display: none; }
details.dash-section > summary::marker { content: ""; }
details.dash-section > summary h2.section-title {
  margin: 24px 0 14px;
  transition: color .15s;
}
details.dash-section[open] > summary h2.section-title { color: var(--accent); }
details.dash-section > summary:hover h2.section-title { color: #fff; }
/* Hide the leading v glyph in markup (visual caret handled below) */

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 14px;
}
.card {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 18px;
  box-shadow: 0 10px 28px rgba(0,0,0,0.14);
  transition: transform .14s ease, border-color .14s ease, background .14s ease;
}
.card:hover { transform: translateY(-1px); border-color: #43516a; background: #121722; }
body.compact .card { padding: 12px 14px; }
body.compact .reason, body.compact .risks, body.compact .headline, body.compact .exit-block { display: none; }
body.compact .grid { padding: 8px 0; gap: 4px 12px; }
.card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; gap: 8px; flex-wrap: wrap; }
.ticker-block { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.ticker { font-size: 18px; font-weight: 700; letter-spacing: -0.5px; }
.badge {
  font-size: 10px; padding: 2px 8px; border-radius: 4px;
  color: #fff; font-weight: 600; letter-spacing: 0.5px;
}
.chip {
  font-size: 10px; padding: 2px 7px; border-radius: 4px;
  background: #1f1f24; color: var(--accent);
}
.trade-status {
  border: 1px solid transparent;
  font-weight: 700;
}
.chip.earn { background: #422006; color: #fbbf24; }
.contract { margin-bottom: 10px; }
.contract-line {
  display: block; font-family: "JetBrains Mono", monospace;
  font-size: 13px; color: var(--accent);
}
.contract .muted { font-size: 11px; }
.grid {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px 16px;
  padding: 10px 0; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
}
.grid .val { font-size: 13px; }
.headline {
  margin-top: 10px; padding: 8px 10px; background: var(--panel-2);
  border-radius: 4px; font-size: 12px; color: var(--accent);
  border-left: 2px solid #fbbf24;
}
.reason, .risks { margin-top: 10px; }
.reason h4, .risks h4 {
  font-size: 10px; text-transform: uppercase; letter-spacing: 1.5px;
  color: var(--muted); margin: 0 0 4px;
}
.reason p, .risks p { margin: 0; font-size: 12.5px; }
.risks p { color: #fca5a5; }
.conf-bar {
  position: relative; width: 80px; height: 22px;
  background: #1f1f24; border-radius: 4px; overflow: hidden;
}
.conf-fill { height: 100%; transition: width .2s; }
.conf-text {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 600; font-family: "JetBrains Mono", monospace;
  text-shadow: 0 1px 2px rgba(0,0,0,0.6);
}

/* Side panels */
.panel-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 16px 0; }
.panel {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 18px; margin-bottom: 16px;
}
.panel h3 {
  margin: 0 0 12px; font-size: 13px; font-weight: 600;
  letter-spacing: 0.3px;
}
.panel h3 .muted { font-weight: 400; font-size: 11px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.sub {
  font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
  color: var(--muted); margin: 0 0 8px;
}

.news-list, .earn-list, .ins-list {
  display: flex; flex-direction: column; gap: 6px;
}
.news-row, .earn-row, .ins-row {
  display: grid; align-items: center; gap: 10px;
  padding: 8px 10px; background: var(--panel-2); border-radius: 6px;
  font-size: 12px;
}
.news-row { grid-template-columns: 60px 1fr 2fr; }
.earn-row { grid-template-columns: 70px 100px 80px 1fr; }
.ins-row  { grid-template-columns: 60px 60px 70px 1fr; }
.news-counts { display: flex; gap: 4px; }
.news-headline { color: var(--accent); font-size: 12px; line-height: 1.4; }
.earn-date { font-family: "JetBrains Mono", monospace; }
.earn-sup, .ins-val { font-family: "JetBrains Mono", monospace; font-size: 11px; }
.ins-score { font-family: "JetBrains Mono", monospace; font-weight: 600; }

.wsb-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 8px;
}
.wsb-tile {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; background: var(--panel-2); border-radius: 6px;
  font-size: 12px;
}
.wsb-ticker { font-weight: 600; }

/* Performance panel */
.perf-headline {
  display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px;
  background: var(--panel-2); border-radius: 6px; padding: 14px 18px;
}
.perf-headline .lab {
  display:block; font-size: 10px; text-transform: uppercase;
  color: var(--muted); letter-spacing: 0.5px; margin-bottom: 4px;
}
.perf-headline .val { font-size: 20px; font-family: "JetBrains Mono", monospace; font-weight: 600; }
.perf-row {
  display: grid; grid-template-columns: 100px 60px 80px 1fr; gap: 10px;
  padding: 8px 10px; background: var(--panel-2); border-radius: 6px;
  font-size: 12px; align-items: center; margin-bottom: 4px;
}
.perf-bucket { font-weight: 600; }
.perf-n, .perf-win, .perf-pnl { font-family: "JetBrains Mono", monospace; }

/* Interactive controls */
.controls {
  position: sticky; top: 0; z-index: 100;
  background: rgba(9,11,16,.92); backdrop-filter: blur(12px);
  padding: 12px 0; margin-bottom: 16px;
  border-bottom: 1px solid var(--border);
  box-shadow: 0 12px 28px rgba(0,0,0,.24);
  display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
}
.controls input, .controls select {
  background: var(--panel-2); color: var(--text);
  border: 1px solid var(--border); border-radius: 6px;
  padding: 8px 12px; font-size: 13px; font-family: inherit;
}
.controls input { min-width: 220px; }
.controls input:focus, .controls select:focus {
  outline: none; border-color: #3b82f6;
}
.controls .filter-chip {
  cursor: pointer; padding: 6px 12px; border-radius: 16px;
  background: var(--panel-2); border: 1px solid var(--border);
  font-size: 12px; user-select: none;
}
.controls .filter-chip.active {
  background: #3b82f6; border-color: #3b82f6; color: #fff;
}
.controls .filter-chip:hover { border-color: #3b82f6; }
.controls .stats {
  margin-left: auto; font-size: 12px; color: var(--muted);
  font-family: "JetBrains Mono", monospace;
}
.lookup-panel {
  display: none; margin: -4px 0 16px; padding: 12px 14px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
}
.lookup-panel.active { display: block; }
.lookup-title {
  display: flex; justify-content: space-between; gap: 12px; align-items: baseline;
  margin-bottom: 8px;
}
.lookup-title strong { font-size: 13px; }
.lookup-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.lookup-hit {
  border: 1px solid var(--border); border-radius: 999px; padding: 6px 10px;
  background: var(--panel-2); font-size: 12px; color: var(--text);
}
.lookup-hit b { color: var(--accent); }
.lookup-hit span { color: var(--muted); margin-left: 6px; }
.control-button {
  cursor: pointer; border: 1px solid var(--border); background: var(--panel-2);
  color: var(--text); border-radius: 6px; padding: 8px 10px;
  font-size: 12px; font-family: inherit;
}
.control-button:hover, .control-button.active { border-color: var(--focus); color: #fff; }
.control-button.primary { background: #2563eb; border-color: #3b82f6; color: #fff; }
.control-separator { width: 1px; align-self: stretch; background: var(--border); margin: 0 2px; }
.toast {
  position: fixed; right: 22px; bottom: 22px; z-index: 200;
  background: #0f172a; border: 1px solid #334155; color: var(--text);
  border-radius: 8px; padding: 10px 12px; box-shadow: 0 18px 40px rgba(0,0,0,.35);
  opacity: 0; transform: translateY(8px); pointer-events: none;
  transition: opacity .16s ease, transform .16s ease; font-size: 12px;
}
.toast.active { opacity: 1; transform: translateY(0); }
.section-nav {
  display: flex; flex-wrap: wrap; gap: 8px; margin: -4px 0 14px;
}
.section-nav a {
  color: var(--muted); text-decoration: none; font-size: 12px;
  border: 1px solid var(--border); border-radius: 999px; padding: 5px 10px;
  background: var(--panel-2);
}
.section-nav a:hover { color: #fff; border-color: var(--focus); }

/* Sizing block on cards */
.sizing-block {
  margin-top: 10px; padding: 10px 12px;
  background: var(--panel-2); border-radius: 6px;
  border-left: 3px solid #3b82f6;
}
.sizing-block.warn { border-left-color: #f59e0b; }
.sizing-block h4 {
  font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
  color: var(--muted); margin: 0 0 6px;
}
.sizing-block h4 .muted { font-size: 9px; text-transform: none; letter-spacing: 0; }
.sizing-block .sizing-row {
  display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
  font-family: "JetBrains Mono", monospace; font-size: 13px;
}
.sizing-block strong { color: var(--accent); font-size: 16px; }
.sizing-block p { margin: 4px 0 0; font-size: 12px; color: #fbbf24; }

/* Exit triggers */
.exit-block {
  margin-top: 10px; padding: 10px 12px;
  background: var(--panel-2); border-radius: 6px;
  border-left: 3px solid #94a3b8;
}
.exit-block h4 {
  font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
  color: var(--muted); margin: 0 0 6px;
}
.exit-row { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; }
.exit-stop { color: #fca5a5; }
.exit-target { color: #86efac; }

/* Congress panel */
.cong-row {
  display: grid; grid-template-columns: 60px 60px 110px 1fr; gap: 10px;
  padding: 8px 10px; background: var(--panel-2); border-radius: 6px;
  font-size: 12px; align-items: center; margin-bottom: 4px;
}
.cong-tk { font-weight: 600; }
.cong-score { font-family: "JetBrains Mono", monospace; font-weight: 600; }
.cong-buyer { font-size: 11px; }
.cong-list { display: flex; flex-direction: column; gap: 4px; }

.card.hidden { display: none; }
.empty-msg {
  padding: 32px 16px; text-align: center; color: var(--muted);
  font-style: italic;
}
.chart-empty {
  height: 100%; min-height: 160px; display: flex; align-items: center; justify-content: center;
  border: 1px dashed #334155; border-radius: 8px; color: #94a3b8;
  background: linear-gradient(135deg, rgba(15,23,42,.7), rgba(30,41,59,.45));
  text-align: center; padding: 18px; font-size: 13px;
}

table.ranked {
  width: 100%; border-collapse: collapse; font-size: 12px;
  font-family: "JetBrains Mono", monospace;
}
table.ranked th, table.ranked td {
  padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: left;
}
table.ranked th {
  color: var(--muted); font-weight: 500; font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
table.ranked tbody tr:hover { background: var(--panel-2); }
table.ranked tbody tr.hidden { display: none; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }

.appendix {
  margin-top: 32px; padding: 20px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
}
.appendix h3 { margin-top: 0; font-size: 13px; }
.appendix code {
  font-family: "JetBrains Mono", monospace; background: #1f1f24;
  padding: 1px 6px; border-radius: 3px; font-size: 12px;
}
.weights { display: grid; grid-template-columns: repeat(2, 1fr); gap: 4px 24px; }
.weights .row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px dotted #2a2a30; }
.demo-banner {
  background: #422006; border: 1px solid #92400e; color: #fbbf24;
  padding: 10px 16px; border-radius: 6px; margin-bottom: 16px;
  font-size: 12px;
}

@media (max-width: 900px) {
  .wrap { padding: 20px 14px 72px; }
  header.top { align-items: flex-start; flex-direction: column; gap: 8px; }
  .macro-grid, .stats-panel, .perf-headline { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .panel-row, .two-col { grid-template-columns: 1fr; }
  .cards { grid-template-columns: 1fr; }
  .controls { top: 0; gap: 8px; }
  .controls input, .controls select { width: 100%; min-width: 0; }
  .controls .stats { margin-left: 0; width: 100%; }
  .control-separator { display: none; }
}
@media print {
  body { background: #fff; color: #111827; }
  .wrap { max-width: none; padding: 12px; }
  .controls, .section-nav, .lookup-panel, .tv-export, .toast { display: none !important; }
  .card, .panel, section.macro, .stats-panel, .chart-box {
    break-inside: avoid; box-shadow: none; border-color: #cbd5e1; background: #fff; color: #111827;
  }
  .muted, .stat-lab, table.ranked th { color: #475569; }
}
.tv-export {
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 16px 20px; margin: 16px 0; font-family: "JetBrains Mono", monospace;
  font-size: 12px;
}
.tv-export h3 { margin: 0 0 8px; font-family: "Inter"; font-size: 14px; }
.tv-export pre {
  margin: 0; padding: 12px; background: var(--panel-2); border-radius: 4px;
  white-space: pre-wrap; max-height: 200px; overflow-y: auto;
}
"""


def _calibration_panel(calib) -> str:
    """Predicted-vs-realized calibration table - shows when to trust pred_return."""
    if not calib:
        return ""
    overall = calib.get("overall") if isinstance(calib, dict) else None
    if not overall or not isinstance(overall, dict):
        return ""
    bins = overall.get("bins")
    summary = overall.get("overall") if isinstance(overall, dict) else None
    if bins is None or (isinstance(bins, pd.DataFrame) and bins.empty):
        # show a deferred state
        return """
<section class="panel">
  <h3>Target Predictor Calibration <span class="muted">(predicted vs realized)</span></h3>
  <p class="muted">Not enough closed signals yet for calibration analysis (need ~30+). Will populate once forward-test pool grows.</p>
</section>"""
    if isinstance(summary, dict):
        rc = summary.get("rank_correlation")
        mae = summary.get("calibration_mae", 0)
        bias = summary.get("avg_bias", 0)
        verdict = summary.get("verdict", "")
        rc_color = "#10b981" if (rc or 0) > 0.15 else "#f59e0b" if (rc or 0) > 0.05 else "#ef4444"
        bias_color = "#10b981" if abs(bias) < 0.05 else "#f59e0b" if abs(bias) < 0.15 else "#ef4444"
    else:
        rc, mae, bias, verdict, rc_color, bias_color = None, 0, 0, "", "#94a3b8", "#94a3b8"
    rows = ""
    for _, r in bins.iterrows():
        pc = "#10b981" if r["realized_mean"] > 0 else "#ef4444"
        biasc = "#10b981" if abs(r["bias"]) < 0.05 else "#f59e0b" if abs(r["bias"]) < 0.15 else "#ef4444"
        rows += f"""
<div class="perf-row">
  <div class="perf-bucket">pred {r['pred_mean']*100:+.1f}%</div>
  <div class="perf-n">n={int(r['n'])}</div>
  <div class="perf-pnl" style="color:{pc}">realized {r['realized_mean']*100:+.2f}%</div>
  <div class="perf-pnl" style="color:{biasc}">bias {r['bias']*100:+.2f}%</div>
</div>"""
    rc_str = f"{rc:+.2f}" if rc is not None else "-"
    return f"""
<section class="panel">
  <h3>Target Predictor Calibration <span class="muted">(predicted vs realized)</span></h3>
  <div class="perf-headline">
    <div><span class="lab">Rank corr</span><span class="val" style="color:{rc_color}">{rc_str}</span></div>
    <div><span class="lab">Cal MAE</span><span class="val">{mae*100:.2f}%</span></div>
    <div><span class="lab">Avg bias</span><span class="val" style="color:{bias_color}">{bias*100:+.2f}%</span></div>
    <div><span class="lab">N signals</span><span class="val">{int(summary.get('n_signals', 0)) if isinstance(summary, dict) else 0}</span></div>
  </div>
  <p class="muted" style="font-size:12px;margin-top:8px;">{html.escape(verdict or '')}</p>
  <h4 class="sub" style="margin-top:12px;">Per-decile breakdown</h4>
  {rows}
</section>"""


def _performance_panel(forward_summary, validation_summary: Optional[Dict] = None) -> str:
    """Render the Performance Tracking panel from lifecycle validation first.

    Forward repricing is useful research telemetry, but it is not the same thing
    as the open/closed trade lifecycle. When validation is available, keep this
    panel aligned with the lifecycle dashboard so a fresh archive/reset does not
    display stale forward-test P&L next to current open positions.
    """
    if validation_summary:
        assets = validation_summary.get("assets", {}) or {}
        closed = int(validation_summary.get("closed_positions") or 0)
        open_count = int(validation_summary.get("open_positions") or 0)
        overall = validation_summary.get("overall", {}) or {}
        avg_ret = overall.get("avg_return")
        med_ret = overall.get("median_return")
        win_rate = overall.get("win_rate")
        pf = overall.get("profit_factor")
        max_dd = overall.get("max_drawdown")
        fixed_horizon = validation_summary.get("fixed_horizon", {}) or {}
        fixed_headline = fixed_horizon.get("headline", {}) or {}
        fixed_shadow = fixed_horizon.get("headline_shadow", {}) or {}
        fixed_sessions = int(fixed_horizon.get("headline_horizon_sessions") or 10)

        def _fmt_pct(value, default="0.0%"):
            if value is None:
                return default
            try:
                return f"{float(value) * 100:+.1f}%"
            except Exception:
                return default

        def _asset_row(asset: str, label: str) -> str:
            row = assets.get(asset, {}) or {}
            overall_row = row.get("overall", {}) or {}
            c = int(row.get("closed_positions") or 0)
            o = int(row.get("open_positions") or 0)
            wr = row.get("win_rate", overall_row.get("win_rate"))
            ar = row.get("avg_return", overall_row.get("avg_return"))
            wr_txt = "n/a" if wr is None else f"{float(wr) * 100:.1f}%"
            ar_txt = _fmt_pct(ar, "n/a")
            color = "#10b981" if (ar or 0) >= 0 else "#ef4444"
            return f"""
<div class="perf-row">
  <div class="perf-bucket">{label}</div>
  <div class="perf-n">{o} open / {c} closed</div>
  <div class="perf-win">win {wr_txt}</div>
  <div class="perf-pnl" style="color:{color}">{ar_txt}</div>
</div>"""

        win_color = "#10b981" if (win_rate or 0) >= 0.55 else "#f59e0b" if (win_rate or 0) >= 0.45 else "#ef4444"
        pnl_color = "#10b981" if (avg_ret or 0) >= 0 else "#ef4444"
        fixed_win = fixed_shadow.get("win_rate")
        fixed_avg = fixed_shadow.get("avg_return")
        fixed_excess = fixed_shadow.get("avg_excess_vs_spy")
        fixed_html = f"""
      <h4 class="sub">Independent {fixed_sessions}-session evidence</h4>
      <div class="perf-row">
        <div class="perf-bucket">SHADOW</div>
        <div class="perf-n">n={int(fixed_shadow.get('n') or 0)} / {int(fixed_shadow.get('unique_entry_days') or 0)} days</div>
        <div class="perf-win">win {'n/a' if fixed_win is None else f'{float(fixed_win)*100:.1f}%'}</div>
        <div class="perf-pnl">{_fmt_pct(fixed_avg, 'n/a')}</div>
      </div>
      <p class="muted" style="font-size:11px;margin-top:6px;">Executed n={int(fixed_headline.get('n') or 0)}. Average shadow excess vs SPY: {_fmt_pct(fixed_excess, 'n/a')}. Shadow rows passed strategy rules before portfolio guardrails. Options use a labeled constant-entry-IV proxy; shares and futures use observed closes.</p>
        """ if fixed_horizon else ""
        return f"""
<section class="panel">
  <h3>Signal Performance Tracking <span class="muted">(lifecycle validation)</span></h3>
  <div class="perf-headline">
    <div><span class="lab">Open</span><span class="val">{open_count}</span></div>
    <div><span class="lab">Closed</span><span class="val">{closed}</span></div>
    <div><span class="lab">Win rate</span><span class="val" style="color:{win_color}">{'n/a' if win_rate is None else f'{float(win_rate)*100:.1f}%'}</span></div>
    <div><span class="lab">Avg P&amp;L</span><span class="val" style="color:{pnl_color}">{_fmt_pct(avg_ret)}</span></div>
    <div><span class="lab">Median</span><span class="val">{_fmt_pct(med_ret)}</span></div>
  </div>
  <div class="perf-headline" style="margin-top:14px;">
    <div><span class="lab">Profit factor</span><span class="val">{'n/a' if pf is None else f'{float(pf):.2f}'}</span></div>
    <div><span class="lab">Max DD</span><span class="val" style="color:#ef4444">{_fmt_pct(max_dd)}</span></div>
    <div><span class="lab">Scope</span><span class="val">current</span></div>
  </div>
  <div class="two-col" style="margin-top:14px;">
    <div>
      <h4 class="sub">By asset class</h4>
      {_asset_row('option', 'OPTIONS')}
      {_asset_row('share', 'SHARES')}
      {_asset_row('futures', 'FUTURES')}
    </div>
    <div>
      <h4 class="sub">Why this may be empty</h4>
      <p class="muted">Closed P&amp;L starts at zero after an archive/reset and fills in only when lifecycle positions close. Forward-reprice history is kept separate so stale paper history cannot mix into the current experiment.</p>
      {fixed_html}
    </div>
  </div>
</section>
"""
    if not forward_summary or forward_summary.get("signals", pd.DataFrame()).empty:
        return f"""
<section class="panel">
  <h3>Signal Performance Tracking <span class="muted">(auto-updated each run)</span></h3>
  <p class="muted">No tracked signals yet. Run <code>python run.py</code> daily and a track record will accumulate here automatically. Each ranked trade is logged to <code>logs/signals_*.parquet</code> and re-priced on every subsequent run.</p>
</section>
"""
    ovr = forward_summary["overall"]
    by_conf = forward_summary.get("by_confidence", pd.DataFrame())
    by_type = forward_summary.get("by_type", pd.DataFrame())
    win_color = "#10b981" if ovr["win_rate"] >= 0.55 else "#f59e0b" if ovr["win_rate"] >= 0.45 else "#ef4444"
    pnl_color = "#10b981" if ovr["avg_pnl_pct"] > 0 else "#ef4444"

    conf_rows = ""
    if not by_conf.empty:
        for _, r in by_conf.iterrows():
            wc = "#10b981" if r["win_rate"] >= 0.55 else "#f59e0b" if r["win_rate"] >= 0.45 else "#ef4444"
            pc = "#10b981" if r["avg_pnl"] > 0 else "#ef4444"
            conf_rows += f"""
<div class="perf-row">
  <div class="perf-bucket">{html.escape(r['bucket'])}</div>
  <div class="perf-n">n={int(r['n'])}</div>
  <div class="perf-win" style="color:{wc}">win {r['win_rate']*100:.0f}%</div>
  <div class="perf-pnl" style="color:{pc}">{r['avg_pnl']*100:+.2f}%</div>
</div>"""

    type_rows = ""
    if not by_type.empty:
        for _, r in by_type.iterrows():
            wc = "#10b981" if r["win_rate"] >= 0.55 else "#f59e0b" if r["win_rate"] >= 0.45 else "#ef4444"
            pc = "#10b981" if r["avg_pnl"] > 0 else "#ef4444"
            type_rows += f"""
<div class="perf-row">
  <div class="perf-bucket">{html.escape(r['type']).upper()}</div>
  <div class="perf-n">n={int(r['n'])}</div>
  <div class="perf-win" style="color:{wc}">win {r['win_rate']*100:.0f}%</div>
  <div class="perf-pnl" style="color:{pc}">{r['avg_pnl']*100:+.2f}%</div>
</div>"""

    # New: per-asset-type breakdown
    by_asset = forward_summary.get("by_asset", pd.DataFrame())
    asset_rows = ""
    if not by_asset.empty:
        for _, r in by_asset.iterrows():
            wc = "#10b981" if r["win_rate"] >= 0.55 else "#f59e0b" if r["win_rate"] >= 0.45 else "#ef4444"
            pc = "#10b981" if r["avg_pnl"] > 0 else "#ef4444"
            sharpe_str = f"  Sharpe {r.get('sharpe', 0):.2f}" if r.get("sharpe") else ""
            asset_rows += f"""
<div class="perf-row">
  <div class="perf-bucket">{html.escape(r['asset']).upper()}</div>
  <div class="perf-n">n={int(r['n'])}</div>
  <div class="perf-win" style="color:{wc}">win {r['win_rate']*100:.0f}%</div>
  <div class="perf-pnl" style="color:{pc}">{r['avg_pnl']*100:+.2f}%{sharpe_str}</div>
</div>"""

    # Drop reasons (signals that couldn't be re-priced)
    drops = forward_summary.get("dropped", {})
    drop_info = ""
    if drops:
        drop_str = ", ".join(f"{k}:{v}" for k, v in drops.items())
        drop_info = f'<p class="muted" style="margin-top:8px;font-size:11px;">Dropped {sum(drops.values())} signals: {html.escape(drop_str)}</p>'

    # Risk metrics
    risk = forward_summary.get("risk", {}) or {}
    risk_info = ""
    if risk and risk.get("n", 0) >= 5:
        sharpe = risk.get("sharpe", 0)
        sortino = risk.get("sortino", 0)
        dd = risk.get("max_drawdown_pct", 0)
        risk_info = f"""
<div class="perf-headline" style="margin-top:14px;">
  <div><span class="lab">Sharpe</span><span class="val" style="color:{'#10b981' if sharpe > 0.5 else '#f59e0b'}">{sharpe:+.2f}</span></div>
  <div><span class="lab">Sortino</span><span class="val" style="color:{'#10b981' if sortino > 0.5 else '#f59e0b'}">{sortino:+.2f}</span></div>
  <div><span class="lab">Max DD</span><span class="val" style="color:#ef4444">{dd*100:+.1f}%</span></div>
  <div><span class="lab">N</span><span class="val">{risk.get('n', 0)}</span></div>
  <div><span class="lab">Tot logged</span><span class="val">{ovr.get('n_total_logged', '-')}</span></div>
</div>"""

    return f"""
<section class="panel">
  <h3>Signal Performance Tracking <span class="muted">({int(ovr['n_signals'])} signals re-priced)</span></h3>
  <div class="perf-headline">
    <div><span class="lab">Win rate</span><span class="val" style="color:{win_color}">{ovr['win_rate']*100:.1f}%</span></div>
    <div><span class="lab">Avg P&amp;L</span><span class="val" style="color:{pnl_color}">{ovr['avg_pnl_pct']*100:+.2f}%</span></div>
    <div><span class="lab">Median</span><span class="val">{ovr['median_pnl_pct']*100:+.2f}%</span></div>
    <div><span class="lab">Best</span><span class="val" style="color:#10b981">{ovr['best']*100:+.1f}%</span></div>
    <div><span class="lab">Worst</span><span class="val" style="color:#ef4444">{ovr['worst']*100:+.1f}%</span></div>
  </div>
  {risk_info}
  <div class="two-col" style="margin-top:14px;">
    <div>
      <h4 class="sub">By asset class</h4>
      {asset_rows or '<p class="muted">No asset-class data yet.</p>'}
      <h4 class="sub" style="margin-top:12px;">By confidence</h4>
      {conf_rows or '<p class="muted">No bucketed data yet.</p>'}
    </div>
    <div>
      <h4 class="sub">By signal type</h4>
      {type_rows or '<p class="muted">No bucketed data yet.</p>'}
    </div>
  </div>
  {drop_info}
</section>
"""


def _analyst_panel(analyst: pd.DataFrame, top_n: int = 10) -> str:
    """Top tickers by analyst sentiment + momentum (Finnhub data)."""
    if analyst is None or analyst.empty:
        return ""
    df = analyst.copy()
    df = df[df["analyst_total"] >= 5]   # need at least 5 analysts to be meaningful
    if df.empty:
        return ""
    # Top buys ranked by score
    bulls = df[df["analyst_score"] > 0.5].sort_values("analyst_score", ascending=False).head(top_n)
    # Recent upgrades (positive momentum)
    upgrades = df[df["analyst_momentum"] > 0].sort_values("analyst_momentum", ascending=False).head(top_n)
    bears = df[df["analyst_score"] < -0.3].sort_values("analyst_score").head(min(top_n // 2, 5))

    def _row(r, color):
        sb = _safe_int(r.get("analyst_strong_buy"))
        b = _safe_int(r.get("analyst_buy"))
        h = _safe_int(r.get("analyst_hold"))
        s = _safe_int(r.get("analyst_sell")) + _safe_int(r.get("analyst_strong_sell"))
        mom = _safe_int(r.get("analyst_momentum"))
        return f"""
<div class="cong-row">
  <div class="cong-tk"><strong>{html.escape(r['ticker'])}</strong></div>
  <div class="cong-score" style="color:{color}">{r['analyst_score']:+.1f}</div>
  <div class="cong-counts"><span class="chip">{sb}SB / {b}B / {h}H / {s}S</span></div>
  <div class="cong-buyer muted">{'+' if mom > 0 else ''}{mom} this month</div>
</div>"""

    bulls_html = "".join(_row(r, "#10b981") for _, r in bulls.iterrows()) or "<p class='muted'>No strong buys</p>"
    bears_html = "".join(_row(r, "#ef4444") for _, r in bears.iterrows()) or "<p class='muted'>No clear bears</p>"
    return f"""
<section class="panel">
  <h3>Analyst Recommendations <span class="muted">(Finnhub, latest month consensus)</span></h3>
  <div class="two-col">
    <div>
      <h4 class="sub">Top buy ratings</h4>
      <div class="cong-list">{bulls_html}</div>
    </div>
    <div>
      <h4 class="sub">Top sell ratings</h4>
      <div class="cong-list">{bears_html}</div>
    </div>
  </div>
</section>
"""


def _social_panel(social: pd.DataFrame, top_n: int = 10) -> str:
    """StockTwits top sentiment + Trump's recent posts touching tickers."""
    if social is None or social.empty:
        return ""
    df = social.copy()
    df = df[df["social_score"].abs() > 0.05].sort_values("social_score", ascending=False)
    if df.empty:
        return ""

    # Top StockTwits-driven tickers (left column)
    st_top = df[df["stocktwits_n"] > 0].head(top_n)
    st_rows = ""
    for _, r in st_top.iterrows():
        n_bull = _safe_int(r.get("stocktwits_n_bull"))
        n_bear = _safe_int(r.get("stocktwits_n_bear"))
        st_avg = float(r.get("stocktwits_avg_sent") or 0)
        sent_color = "#10b981" if st_avg > 0.05 else "#ef4444" if st_avg < -0.05 else "#94a3b8"
        st_rows += f"""
<div class="cong-row">
  <div class="cong-tk"><strong>{html.escape(r['ticker'])}</strong></div>
  <div class="cong-score" style="color:{sent_color}">{st_avg:+.2f}</div>
  <div class="cong-counts"><span class="chip">{_safe_int(r.get('stocktwits_n'))} msgs</span></div>
  <div class="cong-buyer muted">{n_bull} / {n_bear}</div>
</div>"""

    # Trump posts touching tickers (right column)
    tr_df = df[df["trump_n"] > 0].sort_values("trump_avg_sent", ascending=False).head(top_n)
    tr_rows = ""
    for _, r in tr_df.iterrows():
        tr_avg = float(r.get("trump_avg_sent") or 0)
        sent_color = "#10b981" if tr_avg > 0.05 else "#ef4444" if tr_avg < -0.05 else "#94a3b8"
        excerpt = (r.get("trump_excerpt") or "")[:70]
        tr_rows += f"""
<div class="cong-row">
  <div class="cong-tk"><strong>{html.escape(r['ticker'])}</strong></div>
  <div class="cong-score" style="color:{sent_color}">{tr_avg:+.2f}</div>
  <div class="cong-counts"><span class="chip">{_safe_int(r.get('trump_n'))}x mentioned</span></div>
  <div class="cong-buyer muted">"{html.escape(excerpt)}"</div>
</div>"""
    if not tr_rows:
        tr_rows = "<p class='muted'>No Trump posts mentioning tickers in last 14 days</p>"
    if not st_rows:
        st_rows = "<p class='muted'>No StockTwits messages captured</p>"

    return f"""
<section class="panel">
  <h3>Social Social Signal <span class="muted">(StockTwits + Trump Truth Social)</span></h3>
  <div class="two-col">
    <div>
      <h4 class="sub">Trend StockTwits sentiment leaders</h4>
      <div class="cong-list">{st_rows}</div>
    </div>
    <div>
      <h4 class="sub"> Trump posts touching tickers</h4>
      <div class="cong-list">{tr_rows}</div>
    </div>
  </div>
</section>
"""


def _congress_panel(congress: pd.DataFrame, top_n: int = 12) -> str:
    """Top tickers by Congressional net buying."""
    if congress is None or congress.empty:
        return ""
    df = congress.copy()
    df = df[df["congress_score"].abs() > 0.1].sort_values("congress_score", ascending=False)
    if df.empty:
        return ""
    buyers = df.head(top_n)
    sellers = df.sort_values("congress_score").head(min(top_n // 2, 5))

    def _row(r, color):
        n_sens = _safe_int(r.get("congress_n_sens"))
        n_reps = _safe_int(r.get("congress_n_reps"))
        buyer = (r.get("congress_top_buyer") or "")[:38]
        members = []
        if n_sens > 0: members.append(f"{n_sens}  sen")
        if n_reps > 0: members.append(f"{n_reps}  rep")
        members_str = ", ".join(members) if members else ""
        return f"""
<div class="cong-row">
  <div class="cong-tk"><strong>{html.escape(r['ticker'])}</strong></div>
  <div class="cong-score" style="color:{color}">{r['congress_score']:+.2f}</div>
  <div class="cong-counts"><span class="chip">{members_str}</span></div>
  <div class="cong-buyer muted">{html.escape(buyer)}</div>
</div>"""
    buyers_html = "".join(_row(r, "#10b981") for _, r in buyers.iterrows()) or "<p class='muted'>No notable buys</p>"
    sellers_html = "".join(_row(r, "#ef4444") for _, r in sellers.iterrows()) or "<p class='muted'>No notable sells</p>"
    return f"""
<section class="panel">
  <h3> Congressional Activity <span class="muted">(STOCK Act disclosures, last 90d)</span></h3>
  <div class="two-col">
    <div>
      <h4 class="sub">Top net Congressional buyers</h4>
      <div class="cong-list">{buyers_html}</div>
    </div>
    <div>
      <h4 class="sub">Top net Congressional sellers</h4>
      <div class="cong-list">{sellers_html}</div>
    </div>
  </div>
</section>
"""


def _build_analytics_html(forward_summary=None) -> str:
    """Build the Plotly-powered analytics section using live data from disk."""
    import json
    import warnings
    warnings.filterwarnings("ignore")

    def _load_json_rows(filename: str, asset: str) -> list:
        try:
            rows = json.loads((ROOT / "data" / filename).read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(rows, list):
            return []
        cleaned = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item.setdefault("asset", asset)
            if "ticker" not in item:
                item["ticker"] = item.get("symbol") or item.get("name") or asset
            if "side" not in item:
                item["side"] = item.get("direction") or asset
            cleaned.append(item)
        return cleaned

    validation_summary = {}
    try:
        validation_summary = json.loads((ROOT / "data" / "validation_summary.json").read_text(encoding="utf-8"))
    except Exception:
        validation_summary = {}

    # 1. Load lifecycle closed positions only. Forward reprice files are separate
    # research telemetry and can be stale after archive/reset.
    closed_rows = (
        _load_json_rows("closed_positions.json", "option")
        + _load_json_rows("closed_share_positions.json", "share")
        + _load_json_rows("closed_futures_positions.json", "futures")
    )
    closed_rows = _dedupe_position_rows(closed_rows)
    closed = pd.DataFrame(closed_rows)
    analytics_source = "lifecycle"
    if not closed.empty:
        if "pnl_pct" not in closed.columns:
            closed["pnl_pct"] = pd.NA
        for col in ("current_pnl_pct", "return_pct", "unrealized_pct"):
            if col in closed.columns:
                closed["pnl_pct"] = closed["pnl_pct"].fillna(closed[col])
        closed["pnl_pct"] = pd.to_numeric(closed["pnl_pct"], errors="coerce").fillna(0.0)
        if "exit_time" in closed.columns:
            closed["log_time"] = pd.to_datetime(closed["exit_time"], errors="coerce", utc=True)
        elif "entry_time" in closed.columns:
            closed["log_time"] = pd.to_datetime(closed["entry_time"], errors="coerce", utc=True)
        else:
            closed["log_time"] = pd.Timestamp.utcnow()
        closed["log_time"] = closed["log_time"].fillna(pd.Timestamp.utcnow())
        closed["date_str"] = closed["log_time"].dt.strftime("%Y-%m-%d")
        closed["is_win"] = closed["pnl_pct"].map(_is_win_pnl)
        closed["outcome"] = closed.apply(_exit_bucket, axis=1)
        if "bucket" not in closed.columns:
            closed["bucket"] = closed.get("asset", pd.Series("position", index=closed.index)).fillna("position")
        closed = closed.sort_values("log_time")
        cutoff_raw = validation_summary.get("current_model_cutoff") if validation_summary else None
        if validation_summary.get("validation_scope") == "current_model" and cutoff_raw:
            cutoff = pd.to_datetime(cutoff_raw, errors="coerce", utc=True)
            if not pd.isna(cutoff):
                closed = closed[closed["log_time"] >= cutoff].copy()
    else:
        closed = pd.DataFrame(columns=["log_time", "date_str", "outcome", "pnl_pct", "bucket"])

    if closed.empty and forward_summary and not forward_summary.get("signals", pd.DataFrame()).empty:
        closed = forward_summary["signals"].copy()
        analytics_source = "forward"
        if "pnl_pct" not in closed.columns:
            closed["pnl_pct"] = pd.NA
        closed["pnl_pct"] = pd.to_numeric(closed["pnl_pct"], errors="coerce").fillna(0.0)
        if "entry_time" in closed.columns:
            closed["log_time"] = pd.to_datetime(closed["entry_time"], errors="coerce", utc=True)
        else:
            closed["log_time"] = pd.Timestamp.utcnow()
        closed["log_time"] = closed["log_time"].fillna(pd.Timestamp.utcnow())
        closed["date_str"] = closed["log_time"].dt.strftime("%Y-%m-%d")
        closed["outcome"] = closed["pnl_pct"].map(lambda v: "target" if v > 0 else "stop")
        closed["is_win"] = closed["pnl_pct"].map(_is_win_pnl)
        if "bucket" not in closed.columns:
            if "asset" in closed.columns:
                closed["bucket"] = closed["asset"]
            elif "side" in closed.columns:
                closed["bucket"] = closed["side"]
            else:
                closed["bucket"] = "signal"
        closed = closed.sort_values("log_time")

    # 2. Load current open lifecycle positions across all asset classes.
    op_list = (
        _load_json_rows("open_positions.json", "option")
        + _load_json_rows("open_share_positions.json", "share")
        + _load_json_rows("open_futures_positions.json", "futures")
    )
    op_list = _dedupe_position_rows(op_list)
    df_open = pd.DataFrame(op_list)
    try:
        if not df_open.empty and "entry_time" in df_open.columns:
            df_open["entry_time"] = pd.to_datetime(df_open["entry_time"], utc=True)
            df_open["entry_date"] = df_open["entry_time"].dt.strftime("%Y-%m-%d")
    except Exception:
        pass

    #  4. Compute PnL curve
    # Closed lifecycle records currently store trade-level percent returns,
    # not reliable account-weighted dollars. Plot a running average instead
    # of summing/compounding trade percentages into a fake account curve.
    if not closed.empty:
        closed["running_avg_pnl_pct"] = closed["pnl_pct"].expanding().mean()
        daily_pnl = (
            closed.groupby("date_str")
            .agg(curve_pnl=("running_avg_pnl_pct", "last"),
                 trades=("pnl_pct", "count"),
                 avg_pnl=("pnl_pct", "mean"),
                 wins=("is_win", "sum"))
            .reset_index()
        )
        daily_pnl["win_rate"] = (daily_pnl["wins"] / daily_pnl["trades"] * 100).round(1)
        pnl_dates = daily_pnl["date_str"].tolist()
        pnl_cumulative = [round(v * 100, 2) for v in daily_pnl["curve_pnl"].tolist()]
        pnl_daily = [round(v * 100, 2) for v in daily_pnl["avg_pnl"].tolist()]
        pnl_trades = daily_pnl["trades"].tolist()
        pnl_wr = daily_pnl["win_rate"].tolist()
    else:
        pnl_dates = pnl_cumulative = pnl_daily = pnl_trades = pnl_wr = []

    #  5. Win rate by bucket 
    if not closed.empty:
        bucket_stats = (
            closed.groupby("bucket")
            .agg(wins=("is_win", "sum"),
                 total=("outcome", "count"),
                 avg_pnl=("pnl_pct", "mean"))
            .reset_index()
        )
        bucket_stats["win_rate"] = (bucket_stats["wins"] / bucket_stats["total"] * 100).round(1)
        bucket_labels = bucket_stats["bucket"].str.replace("_", " ").str.title().tolist()
        bucket_wr = bucket_stats["win_rate"].tolist()
        bucket_avg = [round(v * 100, 2) for v in bucket_stats["avg_pnl"].tolist()]
    else:
        bucket_labels = bucket_wr = bucket_avg = []

    #  6. Confidence vs win rate scatter 
    if not closed.empty and "confidence" in closed.columns:
        conf_bins = pd.cut(closed["confidence"], bins=[0, 55, 65, 75, 85, 100], labels=["<55", "55-65", "65-75", "75-85", ">85"])
        conf_wr = (
            closed.groupby(conf_bins, observed=True)
            .apply(lambda g: pd.Series({
                "win_rate": g["is_win"].mean() * 100,
                "n": len(g),
                "avg_pnl": g["pnl_pct"].mean() * 100
            }))
            .reset_index()
        )
        conf_labels = conf_wr["confidence"].astype(str).tolist()
        conf_wr_vals = [round(v, 1) for v in conf_wr["win_rate"].tolist()]
        conf_n = conf_wr["n"].tolist()
        conf_pnl = [round(v, 2) for v in conf_wr["avg_pnl"].tolist()]
    else:
        conf_labels = conf_wr_vals = conf_n = conf_pnl = []

    #  7. Factor importance (IC) from the independent-swing validation sample.
    factor_labels, factor_ic, factor_reliability, factor_n, factor_days = [], [], [], [], []
    try:
        ic_rows = json.loads((ROOT / "data" / "factor_ic_summary.json").read_text(encoding="utf-8"))
    except Exception:
        ic_rows = []
    if ic_rows:
        for r in ic_rows[:18]:
            factor_labels.append(str(r.get("factor", "")).replace("_", " ").title())
            factor_ic.append(round(float(r.get("ic") or 0), 4))
            factor_reliability.append(str(r.get("reliability") or "insufficient_history"))
            factor_n.append(int(r.get("n") or 0))
            factor_days.append(int(r.get("trading_days") or 0))

    #  8. Open positions unrealized distribution
    display_open_rows = []
    if not df_open.empty:
        if "unrealized_pct" not in df_open.columns:
            df_open["unrealized_pct"] = pd.NA
        for col in ("current_pnl_pct", "pnl_pct"):
            if col in df_open.columns:
                df_open["unrealized_pct"] = df_open["unrealized_pct"].fillna(df_open[col])
        df_open["unrealized_pct"] = pd.to_numeric(df_open["unrealized_pct"], errors="coerce").fillna(0.0)

        def _num(value, default=0.0):
            try:
                if value is None or pd.isna(value):
                    return default
                return float(value)
            except Exception:
                return default

        for r in op_list:
            current_price = r.get("current_mid", r.get("current_price", r.get("last_price", 0)))
            side = str(r.get("side") or r.get("direction") or r.get("asset") or "-").upper()
            display_open_rows.append({
                "ticker": r.get("ticker") or r.get("symbol") or "-",
                "position_label": _open_position_label(r),
                "asset": r.get("asset") or "position",
                "side": side,
                "strike": r.get("strike", r.get("contract", "-")),
                "expiry": r.get("expiry", "-"),
                "entry_price": _num(r.get("entry_price")),
                "current_price": _num(current_price),
                "unrealized_pct": _num(r.get("unrealized_pct", r.get("current_pnl_pct", r.get("pnl_pct", 0)))),
                "age_days": _num(r.get("age_days")),
                "confidence": r.get("confidence"),
                "stop_price": _num(r.get("stop_price")),
                "target_price": _num(r.get("target_price")),
            })
        display_open_rows = sorted(
            display_open_rows,
            key=lambda row: float(row.get("unrealized_pct") or 0),
            reverse=True,
        )

    if not df_open.empty and "unrealized_pct" in df_open.columns:
        unr_vals = df_open["unrealized_pct"].dropna().tolist()
        unr_labels = [_open_position_label(r) for r in op_list]
        unr_sides = df_open["side"].tolist() if "side" in df_open.columns else ["call"] * len(unr_vals)
        # sort by unrealized_pct desc
        combined = sorted(zip(unr_vals, unr_labels, unr_sides), reverse=True)
        unr_vals, unr_labels, unr_sides = ([x[0] for x in combined],
                                            [x[1] for x in combined],
                                            [x[2] for x in combined])
        open_total_unrealized = round(sum(unr_vals) / len(unr_vals) * 100 if unr_vals else 0, 2)
        total_open = len(unr_vals)
        gainers = sum(1 for v in unr_vals if v > 0)
        losers = total_open - gainers
    else:
        unr_vals = unr_labels = unr_sides = []
        open_total_unrealized = total_open = gainers = losers = 0

    age_labels, age_counts, age_avg = [], [], []
    if not df_open.empty and "entry_time" in df_open.columns:
        if "age_days" not in df_open.columns:
            df_open["age_days"] = (
                pd.Timestamp.utcnow() - pd.to_datetime(df_open["entry_time"], errors="coerce", utc=True)
            ).dt.total_seconds() / 86400.0
        age_bins = [-0.01, 1, 3, 7, 14, 30, float("inf")]
        age_names = ["0-1d", "1-3d", "3-7d", "7-14d", "14-30d", "30d+"]
        df_open["age_bucket"] = pd.cut(df_open["age_days"].clip(lower=0), bins=age_bins, labels=age_names)
        age_stats = df_open.groupby("age_bucket", observed=True).agg(
            count=("ticker", "count"),
            avg_unrealized=("unrealized_pct", "mean") if "unrealized_pct" in df_open.columns else ("ticker", "size"),
        ).reset_index()
        age_labels = age_stats["age_bucket"].astype(str).tolist()
        age_counts = [int(v) for v in age_stats["count"].tolist()]
        age_avg = [
            None if pd.isna(v) else round(float(v) * 100, 2)
            for v in age_stats["avg_unrealized"].tolist()
        ]

    #  9. Outcome pie 
    if not closed.empty:
        oc = closed["outcome"].value_counts()
        pie_labels = oc.index.tolist()
        pie_values = oc.values.tolist()
    else:
        pie_labels = pie_values = []

    #  10. Overall stats 
    total_closed = len(closed) if not closed.empty else 0
    overall_wr = round(closed["is_win"].mean() * 100, 1) if not closed.empty and "is_win" in closed.columns else 0
    overall_avg_pnl = round(closed["pnl_pct"].mean() * 100, 2) if not closed.empty else 0
    median_pnl = round(closed["pnl_pct"].median() * 100, 2) if not closed.empty else 0
    pnl_scope_label = "re-priced" if analytics_source == "forward" else "closed"
    if validation_summary and analytics_source == "lifecycle":
        overall = validation_summary.get("overall", {})
        if validation_summary.get("closed_positions") is not None:
            total_closed = int(validation_summary.get("closed_positions") or 0)
        if overall.get("win_rate") is not None:
            overall_wr = round(float(overall["win_rate"]) * 100, 1)
        if overall.get("avg_return") is not None:
            overall_avg_pnl = round(float(overall["avg_return"]) * 100, 2)

    # JSON-serialize for JS
    import json as _json
    J = _json.dumps

    def _render_open_position_row(row):
        position_label = html.escape(str(row.get("position_label") or row.get("ticker", "-")))
        side = str(row.get("side", "-")).upper()
        side_color = "#10b981" if side.lower() in {"call", "long", "share"} else "#f87171"
        strike = html.escape(str(row.get("strike", "-")))
        expiry = html.escape(str(row.get("expiry", "-")))
        entry_price = float(row.get("entry_price", 0) or 0)
        current_price = float(row.get("current_price", 0) or 0)
        unrealized_pct = float(row.get("unrealized_pct", 0) or 0)
        pnl_color = "#10b981" if unrealized_pct >= 0 else "#ef4444"
        age_days = float(row.get("age_days", 0) or 0)
        confidence = int(row.get("confidence", 0)) if row.get("confidence") else "-"
        stop_price = float(row.get("stop_price", 0) or 0)
        target_price = float(row.get("target_price", 0) or 0)
        return (
            "<tr>"
            f"<td><strong>{position_label}</strong></td>"
            f'<td><span style="color:{side_color}">{html.escape(side)}</span></td>'
            f"<td>{strike}</td>"
            f"<td>{expiry}</td>"
            f"<td>${entry_price:.2f}</td>"
            f"<td>${current_price:.2f}</td>"
            f'<td style="color:{pnl_color};font-weight:600">{unrealized_pct * 100:+.1f}%</td>'
            f"<td>{age_days:.1f}d</td>"
            f"<td>{confidence}</td>"
            f'<td style="color:#f59e0b">${stop_price:.2f}</td>'
            f'<td style="color:#10b981">${target_price:.2f}</td>'
            "</tr>"
        )

    # Keep nested row templating outside the surrounding page f-string so the
    # module parses identically on every supported Python version, including 3.11.
    open_positions_rows_html = "".join(
        _render_open_position_row(row) for row in display_open_rows
    )

    return f"""
<details class="dash-section" open id="sect-analytics">
<summary><h2 class="section-title" style="display:flex;align-items:center;gap:12px;">
  Live Signal Analytics <span class="muted" style="font-size:13px;font-weight:400;">P&amp;L  -  Win Rates  -  Factor IC  -  Open Positions</span>
</h2></summary>

<style>
.analytics-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 18px;
  margin: 18px 0 24px;
}}
.stat-ribbon {{
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 18px;
}}
.stat-chip {{
  background: #0f172a;
  border: 1px solid #1e293b;
  border-radius: 10px;
  padding: 14px 20px;
  min-width: 130px;
  flex: 1;
}}
.stat-chip .sc-val {{
  font-size: 28px;
  font-weight: 700;
  font-family: 'JetBrains Mono', monospace;
  line-height: 1.1;
}}
.stat-chip .sc-lab {{
  font-size: 11px;
  color: #64748b;
  margin-top: 3px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
.chart-box {{
  background: #0a0f1a;
  border: 1px solid #1e293b;
  border-radius: 12px;
  padding: 18px;
  overflow: hidden;
}}
.chart-box h4 {{
  margin: 0 0 12px;
  font-size: 13px;
  color: #94a3b8;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
.positions-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  font-family: 'JetBrains Mono', monospace;
  margin-top: 8px;
}}
.positions-table th {{
  background: #0f172a;
  color: #64748b;
  padding: 7px 10px;
  text-align: left;
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  position: sticky;
  top: 0;
}}
.positions-table td {{
  padding: 6px 10px;
  border-bottom: 1px solid #1e293b;
  color: #e2e8f0;
}}
.positions-table tr:hover td {{ background: #111827; }}
.pos-scroll {{ max-height: 380px; overflow-y: auto; border-radius: 8px; border: 1px solid #1e293b; }}
</style>

<div class="stat-ribbon">
  <div class="stat-chip">
    <div class="sc-val" style="color:{'#10b981' if overall_avg_pnl >= 0 else '#ef4444'}">{overall_avg_pnl:+.1f}%</div>
    <div class="sc-lab">Avg {pnl_scope_label} P&amp;L</div>
  </div>
  <div class="stat-chip">
    <div class="sc-val" style="color:{'#10b981' if median_pnl >= 0 else '#ef4444'}">{median_pnl:+.0f}%</div>
    <div class="sc-lab">Median {pnl_scope_label} P&amp;L</div>
  </div>
  <div class="stat-chip">
    <div class="sc-val" style="color:{'#10b981' if overall_wr >= 40 else '#f59e0b'}">{overall_wr:.1f}%</div>
    <div class="sc-lab">Win rate ({total_closed} {pnl_scope_label})</div>
  </div>
  <div class="stat-chip">
    <div class="sc-val" style="color:{'#10b981' if open_total_unrealized >= 0 else '#ef4444'}">{open_total_unrealized:+.1f}%</div>
    <div class="sc-lab">Avg unrealized ({total_open} open)</div>
  </div>
  <div class="stat-chip">
    <div class="sc-val" style="color:#10b981">{gainers}</div>
    <div class="sc-lab">Open winners ^</div>
  </div>
  <div class="stat-chip">
    <div class="sc-val" style="color:#ef4444">{losers}</div>
    <div class="sc-lab">Open losers v</div>
  </div>
</div>

<div class="analytics-grid">
  <div class="chart-box" style="grid-column: span 2;">
  <h4>Running average P&amp;L over time {'(current forward reprice)' if analytics_source == 'forward' else '(closed lifecycle)'}</h4>
    <div id="chart-pnl-curve" style="height:260px;"></div>
  </div>
  <div class="chart-box">
    <h4>Win rate by strategy bucket</h4>
    <div id="chart-bucket-wr" style="height:240px;"></div>
  </div>
  <div class="chart-box">
    <h4>Outcome breakdown</h4>
    <div id="chart-outcome-pie" style="height:240px;"></div>
  </div>
  <div class="chart-box">
    <h4>Confidence score vs win rate</h4>
    <div id="chart-conf-wr" style="height:240px;"></div>
  </div>
  <div class="chart-box">
    <h4>Factor IC (independent swing outcomes; gray = insufficient history)</h4>
    <div id="chart-factor-ic" style="height:240px;"></div>
  </div>
  <div class="chart-box">
    <h4>Position aging</h4>
    <div id="chart-position-aging" style="height:240px;"></div>
  </div>
</div>

<div class="chart-box" style="margin-bottom:18px;">
  <h4>Open positions - unrealized P&amp;L by ticker (sorted best->worst)</h4>
  <div id="chart-open-positions" style="height:300px;"></div>
</div>

<div class="chart-box">
  <h4>All open positions ({total_open})</h4>
  <div class="pos-scroll">
  <table class="positions-table">
    <thead><tr>
      <th>Position</th><th>Side</th><th>Strike</th><th>Expiry</th>
      <th>Entry $</th><th>Current $</th><th>Unrealized</th><th>Age</th><th>Conf</th><th>Stop</th><th>Target</th>
    </tr></thead>
    <tbody id="pos-tbody">
    {open_positions_rows_html}
    </tbody>
  </table>
  </div>
</div>

<script>
(function() {{
  function showEmpty(id, text) {{
    var el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = '<div class="chart-empty">' + text + '</div>';
  }}
  if (typeof Plotly === 'undefined') {{
    ['chart-pnl-curve','chart-bucket-wr','chart-outcome-pie','chart-conf-wr','chart-factor-ic','chart-position-aging','chart-open-positions'].forEach(function(id) {{
      showEmpty(id, 'Chart library did not load. Refresh once, or check the CDN/network connection.');
    }});
    return;
  }}
  var DARK = {{ paper_bgcolor:'transparent', plot_bgcolor:'transparent',
    font:{{color:'#94a3b8',family:'JetBrains Mono,monospace',size:11}},
    xaxis:{{gridcolor:'#1e293b',linecolor:'#1e293b',zerolinecolor:'#334155'}},
    yaxis:{{gridcolor:'#1e293b',linecolor:'#1e293b',zerolinecolor:'#334155'}},
    margin:{{t:10,b:40,l:55,r:15}} }};

  // 1. PnL curve
  Plotly.newPlot('chart-pnl-curve', [
    {{ x:{J(pnl_dates)}, y:{J(pnl_cumulative)},
       type:'scatter', mode:'lines+markers', name:'Running avg P&L %',
       line:{{color:'#10b981',width:2.5}},
       marker:{{size:6,color:'#10b981'}},
       fill:'tozeroy', fillcolor:'rgba(16,185,129,0.08)',
       hovertemplate:'%{{x}}<br>Running avg: %{{y:+.1f}}%<extra></extra>' }},
    {{ x:{J(pnl_dates)}, y:{J(pnl_daily)},
       type:'bar', name:'Avg daily P&L %',
       marker:{{color:{J(pnl_daily)}, colorscale:[['0','#ef4444'],['0.5','#374151'],['1','#10b981']],
         cmin:-30, cmax:30 }},
       hovertemplate:'%{{x}}<br>Avg P&L: %{{y:+.1f}}%<extra></extra>',
       yaxis:'y2', opacity:0.6 }}
  ], Object.assign({{}}, DARK, {{
    yaxis2: {{overlaying:'y', side:'right', showgrid:false,
             gridcolor:'#1e293b',linecolor:'#1e293b',zerolinecolor:'#334155',
             font:{{color:'#94a3b8',family:'JetBrains Mono,monospace',size:11}}}},
    legend: {{orientation:'h', y:-0.15, x:0, bgcolor:'transparent'}},
    margin: {{t:10,b:50,l:55,r:55}}
  }}), {{displayModeBar:false, responsive:true}});
  if ({len(pnl_dates)} === 0) showEmpty('chart-pnl-curve', 'No closed outcomes yet. This fills in after positions close or hit stop/target.');

  // 2. Bucket win rates
  var bColors = {J(bucket_wr)}.map(v => v >= 40 ? '#10b981' : v >= 25 ? '#f59e0b' : '#ef4444');
  Plotly.newPlot('chart-bucket-wr', [
    {{ x:{J(bucket_labels)}, y:{J(bucket_wr)},
       type:'bar', marker:{{color:bColors}},
       hovertemplate:'%{{x}}<br>Win rate: %{{y:.1f}}%<extra></extra>' }}
  ], Object.assign({{}}, DARK, {{
    xaxis: Object.assign({{}}, DARK.xaxis, {{type:'category'}}),
    yaxis: Object.assign({{}}, DARK.yaxis, {{title:'Win Rate %', range:[0,100]}}),
    shapes: [{{type:'line', x0:-0.5, x1:{len(bucket_labels)}-0.5, y0:50, y1:50,
              line:{{color:'#475569',dash:'dot',width:1}}}}]
  }}), {{displayModeBar:false, responsive:true}});
  if ({len(bucket_labels)} === 0) showEmpty('chart-bucket-wr', 'No closed strategy buckets yet.');

  // 3. Outcome pie
  Plotly.newPlot('chart-outcome-pie', [
    {{ labels:{J(pie_labels)}, values:{J(pie_values)},
       type:'pie', hole:0.5,
       marker:{{colors:['#3b82f6','#10b981','#ef4444']}},
       textinfo:'label+percent',
       hovertemplate:'%{{label}}: %{{value}} trades<extra></extra>' }}
  ], Object.assign({{}}, DARK, {{margin:{{t:10,b:10,l:10,r:10}}}}),
  {{displayModeBar:false, responsive:true}});
  if ({len(pie_values)} === 0) showEmpty('chart-outcome-pie', 'No closed outcomes yet.');

  // 4. Conf vs win rate
  var cColors = {J(conf_wr_vals)}.map(v => v >= 40 ? '#10b981' : '#ef4444');
  Plotly.newPlot('chart-conf-wr', [
    {{ x:{J(conf_labels)}, y:{J(conf_wr_vals)},
       type:'bar', marker:{{color:cColors}},
       customdata:{J(list(zip(conf_n, conf_pnl)))},
       hovertemplate:'Conf %{{x}}<br>Win rate: %{{y:.1f}}%<br>n=%{{customdata[0]}}<br>Avg P&L: %{{customdata[1]:+.1f}}%<extra></extra>' }}
  ], Object.assign({{}}, DARK, {{
    xaxis: Object.assign({{}}, DARK.xaxis, {{title:'Confidence bucket', type:'category'}}),
    yaxis: Object.assign({{}}, DARK.yaxis, {{title:'Win rate %', range:[0,100]}})
  }}), {{displayModeBar:false, responsive:true}});
  if ({len(conf_labels)} === 0) showEmpty('chart-conf-wr', 'No confidence bucket history yet.');

  // 5. Factor IC
  var icSorted = {J(list(zip(factor_labels, factor_ic, factor_reliability, factor_n, factor_days)))}
    .sort((a,b)=>Math.abs(b[1])-Math.abs(a[1]));
  var icLabels = icSorted.map(x=>x[0]), icVals = icSorted.map(x=>x[1]);
  var icMeta = icSorted.map(x=>[x[2], x[3], x[4]]);
  var icColors = icSorted.map(x => x[2] === 'insufficient_history' ? '#64748b' :
    x[2] === 'supportive' ? '#10b981' : x[2] === 'adverse' ? '#ef4444' : '#f59e0b');
  Plotly.newPlot('chart-factor-ic', [
    {{ x:icVals, y:icLabels, type:'bar', orientation:'h',
       marker:{{color:icColors}},
       customdata:icMeta,
       hovertemplate:'%{{y}}<br>IC: %{{x:.4f}}<br>Reliability: %{{customdata[0]}}<br>n=%{{customdata[1]}} across %{{customdata[2]}} days<extra></extra>' }}
  ], Object.assign({{}}, DARK, {{
    xaxis: Object.assign({{}}, DARK.xaxis, {{title:'Information Coefficient (correlation)', zeroline:true}}),
    yaxis: Object.assign({{}}, DARK.yaxis, {{type:'category'}}),
    margin: {{t:10,b:40,l:100,r:15}}
  }}), {{displayModeBar:false, responsive:true}});
  if (icLabels.length === 0) showEmpty('chart-factor-ic', 'Run validation to calculate independent-swing factor IC.');

  // 6. Position aging
  Plotly.newPlot('chart-position-aging', [
    {{ x:{J(age_labels)}, y:{J(age_counts)}, type:'bar',
       marker:{{color:'#38bdf8'}},
       customdata:{J(age_avg)},
       hovertemplate:'%{{x}}<br>Open positions: %{{y}}<br>Avg unrealized: %{{customdata:+.1f}}%<extra></extra>' }}
  ], Object.assign({{}}, DARK, {{
    xaxis: Object.assign({{}}, DARK.xaxis, {{type:'category'}}),
    yaxis: Object.assign({{}}, DARK.yaxis, {{title:'Open count'}})
  }}), {{displayModeBar:false, responsive:true}});
  if ({len(age_labels)} === 0) showEmpty('chart-position-aging', 'No open-position age history yet.');

  // 7. Open positions bar
  var posColors = {J([v*100 for v in unr_vals])}.map(v => v >= 0 ? '#10b981' : '#ef4444');
  Plotly.newPlot('chart-open-positions', [
    {{ x:{J(unr_labels)}, y:{J([round(v*100,2) for v in unr_vals])},
       type:'bar',
       marker:{{color:posColors}},
       hovertemplate:'%{{x}}<br>Unrealized: %{{y:+.1f}}%<extra></extra>' }}
  ], Object.assign({{}}, DARK, {{
    xaxis: Object.assign({{}}, DARK.xaxis, {{type:'category', tickangle:-45}}),
    yaxis: Object.assign({{}}, DARK.yaxis, {{title:'Unrealized %', zeroline:true}}),
    shapes: [{{type:'line', x0:-0.5, x1:{len(unr_labels)}-0.5, y0:0, y1:0,
              line:{{color:'#475569',width:1}}}}],
    margin: {{t:10,b:60,l:55,r:15}}
  }}), {{displayModeBar:false, responsive:true}});
  if ({len(unr_labels)} === 0) showEmpty('chart-open-positions', 'No open positions have mark-to-market data yet.');
}})();
</script>
</details>
"""


def render(calls: pd.DataFrame, puts: pd.DataFrame, shares: pd.DataFrame,
           ranked_options: pd.DataFrame, ranked_shares: pd.DataFrame,
           macro: Dict[str, Any], asof: datetime, demo: bool = False,
           news: pd.DataFrame = None, earnings: pd.DataFrame = None,
           insider: pd.DataFrame = None, trending: List[str] = None,
           elapsed: float = 0.0, universe_size: int = 0,
           value_plays: pd.DataFrame = None, futures_plays: pd.DataFrame = None,
           forward_summary=None, bankroll: float = 10000,
           aggressive: bool = False, congress: pd.DataFrame = None,
           sentiment: pd.DataFrame = None,
           trending_meta: List = None,
           social: pd.DataFrame = None,
           analyst: pd.DataFrame = None,
           calibration_summary: Optional[Dict[str, Any]] = None,
           # ---- v20 payloads (all optional for back-compat) ------------
           portfolio_greeks: Optional[Dict] = None,
           hedge_suggestion: Optional[Dict] = None,
           breaker_state: Optional[Dict] = None,
           research_guard_report: Optional[Dict] = None,
           engine_timings: Optional[Dict] = None,
           engine_health: Optional[Dict] = None,
           validation_summary: Optional[Dict] = None,
           v20_factors: Optional[Dict] = None,
           empty_engines: Optional[List[Dict]] = None,
           **_unused) -> Path:
    """Build a self-contained HTML cockpit. Returns the path."""
    asof_str = asof.strftime("%Y-%m-%d %H:%M UTC")
    trending = trending or []
    portfolio_greeks = portfolio_greeks or {}
    v20_factors = v20_factors or {}

    cards_calls = "\n".join(_option_card(r) for _, r in calls.iterrows()) if calls is not None and not calls.empty else "<p class='muted'>No long-call ideas pass filters this run.</p>"
    cards_puts = "\n".join(_option_card(r) for _, r in puts.iterrows()) if puts is not None and not puts.empty else "<p class='muted'>No long-put ideas pass filters this run.</p>"
    cards_shares = "\n".join(_share_card(r) for _, r in shares.iterrows()) if shares is not None and not shares.empty else "<p class='muted'>No small-cap share ideas above the score threshold.</p>"
    cards_value = "\n".join(_value_card(r) for _, r in value_plays.iterrows()) if value_plays is not None and not value_plays.empty else "<p class='muted'>No standout value plays above threshold.</p>"
    cards_futures = "\n".join(_futures_card(r) for _, r in futures_plays.iterrows()) if futures_plays is not None and not futures_plays.empty else "<p class='muted'>No futures with directional bias.</p>"
    table_opts = _options_table(pd.concat([calls, puts], ignore_index=True) if (calls is not None and puts is not None) else None)
    table_sh = _shares_table(shares)

    # New panels
    news_panel = _news_flow_panel(news) if news is not None else ""
    earn_panel = _earnings_calendar(earnings) if earnings is not None else ""
    insider_panel = _insider_heatmap(insider) if insider is not None else ""
    # WSB panel - pulls mention counts from trending_meta (WSB engine) and Deltasentiment from sentiment df
    wsb_panel = _wsb_panel(trending, sentiment, trending_meta=trending_meta)

    n_calls = len(calls) if calls is not None else 0
    n_puts = len(puts) if puts is not None else 0
    n_shares = len(shares) if shares is not None else 0
    n_news_rows = len(news) if news is not None else 0
    n_earn_rows = len(earnings[earnings["days_to_earnings"].notna()]) if earnings is not None and not earnings.empty and "days_to_earnings" in earnings.columns else 0

    stats = _stats_panel(elapsed, universe_size, n_calls, n_puts, n_shares,
                         n_news_rows, n_earn_rows, len(trending))

    from config import SIGNAL_WEIGHTS
    weight_rows = "".join(
        f'<div class="row"><span>{k}</span><span>{v:.2f}</span></div>'
        for k, v in SIGNAL_WEIGHTS.items()
    )

    from fusion.rank import to_tv_watchlist
    tv_text = to_tv_watchlist(calls, puts, shares)

    demo_banner = ('<div class="demo-banner"><strong>DEMO/HYBRID MODE</strong> - '
                    'options + sentiment data is synthetic. Insider data is LIVE if SEC EDGAR is reachable. '
                    'Run without <code>--demo</code> on a residential IP for full live mode.</div>') if demo else ""
    guard_banner = ""
    if research_guard_report and research_guard_report.get("warnings"):
        status = html.escape(str(research_guard_report.get("status", "review")).upper())
        rows = "".join(
            f"<li>{html.escape(str(w.get('message', w)))}</li>"
            for w in research_guard_report.get("warnings", [])[:5]
        )
        guard_banner = f"""
  <section class="panel" style="border-left:4px solid #f59e0b">
    <h3>Research Guard <span class="muted">({status})</span></h3>
    <ul style="margin:8px 0 0 18px; color:#cbd5e1">{rows}</ul>
  </section>
"""

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Optedge - Quant Cockpit</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.plot.ly/plotly-2.35.0.min.js" charset="utf-8"></script>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>Optedge - Quant Cockpit</h1>
      <div class="muted">Multi-asset swing research  -  multi-factor fusion  -  policy {STRATEGY_VERSION}</div>
    </div>
    <div class="meta">
      asof {asof_str}
    </div>
  </header>

  {demo_banner}
  {guard_banner}
  {_macro_banner(macro)}
  {stats}
  {_build_analytics_html(forward_summary)}

  <nav class="section-nav" aria-label="Dashboard sections">
    <a href="#sect-analytics">Analytics</a>
    <a href="#sect-calls">Calls</a>
    <a href="#sect-puts">Puts</a>
    <a href="#sect-shares">Shares</a>
    <a href="#sect-value">Value</a>
    <a href="#sect-futures">Futures</a>
    <a href="#sect-telemetry">Engines</a>
  </nav>

  <div class="controls" id="controls">
    <input type="text" id="search-box" placeholder=" Search ticker (e.g., NVDA, TSLA)..." autocomplete="off">
    <select id="sort-by">
      <option value="default">Sort: rank</option>
      <option value="confidence">Sort: confidence v</option>
      <option value="pred">Sort: predicted return v</option>
      <option value="ev">Sort: EV % v</option>
      <option value="kelly">Sort: Kelly % v</option>
      <option value="ticker">Sort: ticker A-Z</option>
    </select>
    <span class="filter-chip active" data-filter="all">all</span>
    <span class="filter-chip" data-filter="ready">ready</span>
    <span class="filter-chip" data-filter="watch">watch</span>
    <span class="filter-chip" data-filter="call">calls only</span>
    <span class="filter-chip" data-filter="put">puts only</span>
    <span class="filter-chip" data-filter="shares">shares only</span>
    <span class="filter-chip" data-filter="value">value</span>
    <span class="filter-chip" data-filter="futures">futures</span>
    <span class="filter-chip" data-filter="high-conf">conf >= 70</span>
    <span class="filter-chip" data-filter="positive-ev">EV &gt; 0</span>
    <span class="filter-chip" data-filter="positive-kelly">Kelly &gt; 0</span>
    <span class="control-separator" aria-hidden="true"></span>
    <button class="control-button" type="button" id="top-only">Top 10</button>
    <button class="control-button" type="button" id="density-toggle">Compact</button>
    <button class="control-button" type="button" id="expand-all">Expand</button>
    <button class="control-button" type="button" id="collapse-all">Collapse</button>
    <button class="control-button" type="button" id="reset-filters">Reset</button>
    <span class="control-separator" aria-hidden="true"></span>
    <button class="control-button primary" type="button" id="download-csv">CSV</button>
    <button class="control-button" type="button" id="download-json">JSON</button>
    <button class="control-button" type="button" id="copy-visible">Copy tickers</button>
    <button class="control-button" type="button" id="print-dashboard">Print</button>
    <span class="stats" id="card-counter">- cards visible</span>
  </div>
  <div class="toast" id="dashboard-toast" role="status" aria-live="polite"></div>
  <div class="lookup-panel" id="lookup-panel" aria-live="polite">
    <div class="lookup-title">
      <strong id="lookup-heading">Lookup</strong>
      <span class="muted" id="lookup-subtitle">Current scan snapshot only</span>
    </div>
    <div class="lookup-grid" id="lookup-results"></div>
  </div>
  <div class="muted" style="font-size:11px; margin-bottom:16px; font-family:'JetBrains Mono', monospace;">
    Bankroll: <strong>${bankroll:,.0f}</strong>  - 
    {'<strong style="color:#f87171">AGGRESSIVE MODE</strong>  -  1/2 Kelly  -  Cap 10% per option / 15% per share' if aggressive else '1/4 Kelly  -  Cap 5% per option / 8% per share'}
  </div>


  <div class="panel-row">
    <div>{news_panel}</div>
    <div>{earn_panel}</div>
  </div>

  {_performance_panel(forward_summary, validation_summary)}
  {_calibration_panel(calibration_summary)}
  {_analyst_panel(analyst)}
  {_congress_panel(congress)}
  {_social_panel(social)}
  {insider_panel}
  {wsb_panel}

  <details class="dash-section" open id="sect-calls">
    <summary><h2 class="section-title">v Long Calls <span class="count">{n_calls}</span></h2></summary>
    <div class="cards">{cards_calls}</div>
  </details>

  <details class="dash-section" open id="sect-puts">
    <summary><h2 class="section-title">v Long Puts <span class="count">{n_puts}</span></h2></summary>
    <div class="cards">{cards_puts}</div>
  </details>

  <details class="dash-section" open id="sect-shares">
    <summary><h2 class="section-title">v Long Shares <span class="count">{n_shares}</span> <span class="muted">(small caps where options aren't liquid enough)</span></h2></summary>
    <div class="cards">{cards_shares}</div>
  </details>

  <details class="dash-section" open id="sect-value">
    <summary><h2 class="section-title">v Value Plays <span class="count">{len(value_plays) if value_plays is not None else 0}</span> <span class="muted">(cheap & quality - Magic Formula + Graham composite)</span></h2></summary>
    <div class="cards">{cards_value}</div>
  </details>

  <details class="dash-section" open id="sect-futures">
    <summary><h2 class="section-title">v Trend Futures Plays <span class="count">{len(futures_plays) if futures_plays is not None else 0}</span> <span class="muted">(equity index, commodities, treasuries, crypto)</span></h2></summary>
    <div class="cards">{cards_futures}</div>
  </details>

  <details class="dash-section" id="sect-ranked-opts">
    <summary><h2 class="section-title">v Ranked option snapshot <span class="muted">(click to expand)</span></h2></summary>
    {table_opts}
  </details>

  <details class="dash-section" id="sect-ranked-shares">
    <summary><h2 class="section-title">v Ranked share snapshot <span class="muted">(click to expand)</span></h2></summary>
    {table_sh}
  </details>

  __V20_PANELS_PLACEHOLDER__

  <section class="tv-export">
    <h3>TradingView watchlist</h3>
    <p class="muted" style="font-family:Inter">Save the text below to <code>optedge_watchlist.txt</code> and import via the Watchlist panel ->  menu -> Import file.</p>
    <pre>{html.escape(tv_text)}</pre>
  </section>

  <section class="appendix">
    <h3>Methodology</h3>
    <p class="muted">EV = P(win) x predicted gain + P(loss) x max loss. P(win) approximated by |delta| for options. Kelly fraction f* = (b - p - q)/b, then x 0.25 (quarter Kelly per research consensus). Hard caps: 5% bankroll per option trade, 8% per share trade. Negative Kelly = the predictor disagrees with the rank -> marked as "skip".</p>
    <p class="muted">Each contract is z-scored cross-sectionally across the configured factor library and combined using the prior weights below. Action-aligned scoring maps directional evidence to calls or puts, while shares and futures use their asset-specific ranking paths. Max one option idea per ticker is retained for diversity.</p>
    <div class="weights">{weight_rows}</div>
    <h3 style="margin-top:20px;">Filters</h3>
    <p class="muted">Discovery profile: options must clear open interest &gt;= {DISCOVERY_PROFILE.min_open_interest}, daily volume &gt;= {DISCOVERY_PROFILE.min_daily_volume}, bid-ask spread &lt;= {DISCOVERY_PROFILE.max_option_spread_pct:.0%}, mid &gt;= ${DISCOVERY_PROFILE.min_option_price:.2f}, and {DISCOVERY_PROFILE.option_min_dte}-{DISCOVERY_PROFILE.option_max_dte} DTE. Swing-execution profile: a separate Robinhood review candidate requires {SWING_EXECUTION_PROFILE.option_min_dte}+ DTE, spread &lt;= {SWING_EXECUTION_PROFILE.max_option_spread_pct:.0%}, fresh quotes, and all validation and account gates. Shares must score &gt;= 0.6 z-units bullish and not already have an option idea.</p>
    <h3 style="margin-top:20px;">Data sources (all free)</h3>
    <p class="muted">yfinance (options, prices, fundamentals, VIX/yields). Reddit JSON (sentiment + WSB trending). SEC EDGAR Form 4 (insider). Google News RSS (news flow). FRED optional for richer macro. None of this is investment advice.</p>
  </section>
</div>
__JS_PLACEHOLDER__
</body>
</html>
"""
    # Insert JS as a plain string so JS template literals (${...}) don't collide
    # with Python f-string syntax.
    js = _INTERACTIVE_JS
    html_doc = html_doc.replace("__JS_PLACEHOLDER__", js)

    # v20 - inject the v20 panels block
    v20_panels = _build_v20_panels_html(
        portfolio_greeks=portfolio_greeks,
        hedge_suggestion=hedge_suggestion,
        breaker_state=breaker_state,
        engine_timings=engine_timings or {},
        engine_health=engine_health or {},
        v20_factors=v20_factors,
        empty_engines=empty_engines,
    )
    html_doc = html_doc.replace("__V20_PANELS_PLACEHOLDER__", v20_panels)

    out_path = ROOT / "data" / f"dashboard_{asof.strftime('%Y%m%d_%H%M%S')}.html"
    out_path.write_text(html_doc, encoding="utf-8")
    return out_path


def _build_v20_panels_html(portfolio_greeks: Dict, hedge_suggestion: Optional[Dict],
                            breaker_state: Optional[Dict], engine_timings: Dict,
                            engine_health: Optional[Dict],
                            v20_factors: Dict,
                            empty_engines: Optional[List[Dict]] = None) -> str:
    """Generate the v20 dashboard panels block - Greeks, breaker, telemetry,
    empty-engine diagnostic, plus quick summaries of the 15 new factor outputs."""
    portfolio_greeks = portfolio_greeks or {}
    v20_factors = v20_factors or {}
    empty_engines = empty_engines or []

    # --- Portfolio Greeks panel ----
    greeks_html = ""
    if portfolio_greeks.get("n_positions", 0) > 0:
        nd = portfolio_greeks.get("net_delta", 0)
        ng = portfolio_greeks.get("net_gamma", 0)
        nt = portfolio_greeks.get("net_theta", 0)
        nv = portfolio_greeks.get("net_vega", 0)
        delta_color = "#10b981" if nd > 0 else "#f87171"
        hedge_html = ""
        if hedge_suggestion:
            hedge_html = (f'<div class="hedge-warn" style="margin-top:10px;padding:10px;'
                           f'background:#fef3c7;border-left:4px solid #f59e0b;color:#92400e;'
                           f'border-radius:6px;font-family:Inter">'
                           f'Warning {html.escape(hedge_suggestion["suggestion"])}</div>')
        greeks_html = f"""
        <details class="dash-section" id="sect-greeks" open>
          <summary><h2 class="section-title">v Portfolio Greeks <span class="muted">(net exposure across {portfolio_greeks['n_positions']} positions)</span></h2></summary>
          <div class="greeks-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0">
            <div class="greek-tile" style="padding:14px;background:#f8fafc;border-radius:8px;border-left:4px solid {delta_color}">
              <div class="muted" style="font-size:12px">Net Delta</div>
              <div style="font-size:24px;font-weight:700;color:{delta_color}">${nd:+,.0f}</div>
              <div class="muted" style="font-size:11px">{'long bias' if nd > 0 else 'short bias' if nd < 0 else 'neutral'}</div>
            </div>
            <div class="greek-tile" style="padding:14px;background:#f8fafc;border-radius:8px">
              <div class="muted" style="font-size:12px">Net Gamma</div>
              <div style="font-size:24px;font-weight:700">{ng:+,.1f}</div>
              <div class="muted" style="font-size:11px"> per $1 move</div>
            </div>
            <div class="greek-tile" style="padding:14px;background:#f8fafc;border-radius:8px">
              <div class="muted" style="font-size:12px">Net Theta</div>
              <div style="font-size:24px;font-weight:700">${nt:+,.0f}</div>
              <div class="muted" style="font-size:11px">$/day decay</div>
            </div>
            <div class="greek-tile" style="padding:14px;background:#f8fafc;border-radius:8px">
              <div class="muted" style="font-size:12px">Net Vega</div>
              <div style="font-size:24px;font-weight:700">${nv:+,.0f}</div>
              <div class="muted" style="font-size:11px">$/1pp IV move</div>
            </div>
          </div>
          {hedge_html}
        </details>
        """

    # --- Breaker panel ----
    breaker_html = ""
    if breaker_state and breaker_state.get("n", 0) > 0:
        bg = "#fee2e2" if breaker_state["multiplier"] < 1.0 else "#dcfce7"
        border = "#dc2626" if breaker_state["multiplier"] < 1.0 else "#16a34a"
        breaker_html = f"""
        <details class="dash-section" id="sect-breaker" open>
          <summary><h2 class="section-title">v Drawdown breaker <span class="muted">(14-day rolling P&L)</span></h2></summary>
          <div style="padding:14px;background:{bg};border-left:4px solid {border};border-radius:6px;margin:10px 0">
            <div style="font-size:18px;font-weight:600">{html.escape(breaker_state['verdict'])}</div>
            <div class="muted" style="margin-top:6px">Kelly multiplier: {breaker_state['multiplier']:.2f}x  -  Avg P&L: {breaker_state['rolling_pnl_pct']*100:+.2f}%  -  Win rate: {breaker_state['rolling_win_rate']*100:.0f}%  -  n={breaker_state['n']}</div>
          </div>
        </details>
        """

    # --- v20 new factor summaries ----
    factor_rows = []
    factor_labels = {
        "cot": ("Trend CoT", "cot_score"),
        "thirteen_f": (" 13F", "thirteen_f_score"),
        "vix_term": (" VIX-term", "vix_term_score"),
        "eia": ("Cong EIA", "eia_score"),
        "wasde": ("Social WASDE", "wasde_score"),
        "buybacks": (" -  Buybacks", "buyback_score"),
        "gtrends": (" G.Trends", "gtrends_score"),
        "form_144": (" Form144", "form_144_score"),
        "whisper": (" Whisper", "whisper_score"),
        "hyperliquid": ("IV HypLqd", "hyperliquid_score"),
        "twitter": (" Twitter", "twitter_score"),
        "r_options": (" r/options", "r_options_score"),
        "yield_curve": ("Analyst Curve", "curve_score"),
        "credit_spread": (" Credit", "credit_score"),
        "cluster_buys": (" Cluster", "cluster_buys_score"),
    }
    for key, (label, score_col) in factor_labels.items():
        df_ = v20_factors.get(key)
        if df_ is None or (hasattr(df_, "empty") and df_.empty):
            factor_rows.append(f'<tr><td>{label}</td><td class="muted">empty</td><td class="muted">-</td></tr>')
            continue
        try:
            n = len(df_)
            top = ""
            if score_col in df_.columns:
                df_sorted = df_.copy()
                df_sorted["abs"] = pd.to_numeric(df_sorted[score_col], errors="coerce").abs()
                df_sorted = df_sorted.sort_values("abs", ascending=False).head(3)
                tops = []
                for _, row in df_sorted.iterrows():
                    tk = row.get("ticker", "?")
                    sc = row.get(score_col, 0)
                    tops.append(f"{tk} ({sc:+.2f})")
                top = ", ".join(tops)
            factor_rows.append(
                f'<tr><td>{label}</td><td>{n} rows</td><td class="muted">{html.escape(top)}</td></tr>'
            )
        except Exception:
            factor_rows.append(f'<tr><td>{label}</td><td class="muted">err</td><td class="muted">-</td></tr>')

    v20_factor_table = "<table class='v20-factor-table' style='width:100%;font-family:Inter;font-size:13px;border-collapse:collapse'><thead><tr><th style='text-align:left;padding:6px'>Factor</th><th style='text-align:left;padding:6px'>Coverage</th><th style='text-align:left;padding:6px'>Top signals</th></tr></thead><tbody>" + "".join(factor_rows) + "</tbody></table>"

    new_factors_html = f"""
    <details class="dash-section" id="sect-v20-factors">
      <summary><h2 class="section-title">Factor coverage <span class="muted">(newer factor engines - click to expand)</span></h2></summary>
      <div style="padding:10px 0">{v20_factor_table}</div>
    </details>
    """

    # --- Engine telemetry + rolling health (v20.7 - w/ SLA breach indicators) ---
    telemetry_body = ""
    if engine_timings:
        # Pull config.ENGINE_SLA_SECONDS for per-engine targets
        try:
            from config import ENGINE_SLA_SECONDS as _SLA
        except Exception:
            _SLA = {}
        max_elapsed = max((t.get("elapsed", 0) for t in engine_timings.values()),
                          default=1.0)
        rows = []
        for name, t in sorted(engine_timings.items(),
                              key=lambda kv: kv[1].get("elapsed", 0), reverse=True):
            elapsed = float(t.get("elapsed", 0))
            sla = _SLA.get(name)
            ok = "" if t.get("ok") else "-"
            # SLA breach flag: orange when over 80% of SLA, red when over SLA
            sla_chip = ""
            if sla:
                pct = elapsed / sla
                if pct >= 1.0:
                    sla_chip = (f"<span style='color:#f87171;margin-left:6px;"
                                 f"font-weight:600'>SLA {sla:.0f}s -</span>")
                elif pct >= 0.8:
                    sla_chip = (f"<span style='color:#f59e0b;margin-left:6px'>"
                                 f"SLA {sla:.0f}s Warning</span>")
            # Latency bar (relative to slowest engine this run)
            bar_pct = int(min(100, elapsed / max_elapsed * 100)) if max_elapsed > 0 else 0
            bar_color = "#10b981" if t.get("ok") else "#f87171"
            if sla and elapsed >= sla:
                bar_color = "#f87171"
            elif sla and elapsed >= 0.8 * sla:
                bar_color = "#f59e0b"
            bar_html = (f"<div style='display:inline-block;width:80px;height:6px;"
                         f"background:#222;border-radius:3px;vertical-align:middle;"
                         f"margin-right:6px'>"
                         f"<div style='width:{bar_pct}%;height:100%;background:{bar_color};"
                         f"border-radius:3px'></div></div>")
            elapsed_color = "#10b981" if t.get("ok") else "#f87171"
            rows.append(
                f"<tr>"
                f"<td style='padding:4px 6px;color:{elapsed_color}'>{ok} {html.escape(name)}{sla_chip}</td>"
                f"<td style='text-align:right;padding:4px 6px'>{bar_html}{elapsed:.1f}s</td>"
                f"<td style='text-align:right;padding:4px 6px' class='muted'>{int(t.get('rows', 0))} rows</td>"
                f"</tr>"
            )
        telemetry_body = f"""
          <div class="chart-box">
          <h4>This run</h4>
          <table style='font-family:Inter;font-size:13px;width:100%;max-width:780px;border-collapse:collapse'><thead>
            <tr style='border-bottom:1px solid #333'><th style='text-align:left;padding:6px'>Engine</th><th style='text-align:right;padding:6px'>Latency</th><th style='text-align:right;padding:6px'>Output</th></tr>
          </thead><tbody>{''.join(rows)}</tbody></table>
          </div>
        """

    health_body = ""
    health_rows = (engine_health or {}).get("engines", []) if isinstance(engine_health, dict) else []
    if health_rows:
        rows = []
        for r in health_rows[:12]:
            score = float(r.get("health_score") or 0)
            color = "#10b981" if score >= 75 else "#f59e0b" if score >= 50 else "#f87171"
            rows.append(
                f"<tr>"
                f"<td style='padding:5px 6px;font-weight:600'>{html.escape(str(r.get('engine')))}</td>"
                f"<td style='padding:5px 6px;color:{color};font-weight:700;text-align:right'>{score:.0f}</td>"
                f"<td style='padding:5px 6px;text-align:right'>{float(r.get('hit_rate') or 0)*100:.0f}%</td>"
                f"<td style='padding:5px 6px;text-align:right'>{float(r.get('ok_rate') or 0)*100:.0f}%</td>"
                f"<td style='padding:5px 6px;text-align:right' class='muted'>{float(r.get('avg_elapsed') or 0):.1f}s</td>"
                f"</tr>"
            )
        health_body = f"""
          <div class="chart-box">
          <h4>Rolling health</h4>
          <table style='font-family:Inter;font-size:13px;width:100%;max-width:780px;border-collapse:collapse'><thead>
            <tr style='border-bottom:1px solid #333'><th style='text-align:left;padding:6px'>Engine</th><th style='text-align:right;padding:6px'>Health</th><th style='text-align:right;padding:6px'>Hit rate</th><th style='text-align:right;padding:6px'>OK rate</th><th style='text-align:right;padding:6px'>Avg latency</th></tr>
          </thead><tbody>{''.join(rows)}</tbody></table>
          </div>
        """

    engines_html = ""
    if telemetry_body or health_body:
        engines_html = f"""
        <details class="dash-section" id="sect-telemetry">
          <summary><h2 class="section-title">Engine runtime <span class="muted">(this run latency + rolling health)</span></h2></summary>
          <div class="analytics-grid">{telemetry_body}{health_body}</div>
        </details>
        """

    # --- Empty engines diagnostic ---
    empty_html = ""
    if empty_engines:
        rows = []
        for e in empty_engines:
            rows.append(f"<tr><td style='padding:6px;font-weight:600'>{html.escape(e['name'])}</td>"
                         f"<td style='padding:6px' class='muted'>{html.escape(e.get('reason', '') or '-')}</td></tr>")
        empty_html = f"""
        <details class="dash-section" id="sect-empty-engines">
          <summary><h2 class="section-title">Empty engines this run <span class="muted">({len(empty_engines)} returned 0 rows - click for diagnosis)</span></h2></summary>
          <table style='font-family:Inter;font-size:13px;width:100%;max-width:900px;border-collapse:collapse'>
            <thead><tr><th style='text-align:left;padding:6px'>Engine</th><th style='text-align:left;padding:6px'>Likely cause</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
          <p class="muted" style="margin-top:10px;font-size:12px;font-family:Inter">
            Use <code>--skip-&lt;name&gt;</code> CLI flag to disable any of these (e.g. <code>--skip-cot --skip-13f</code>). Or install missing deps: <code>pip install pytrends</code>. EIA/credit/yield-curve need keys in keys.py (FRED_API_KEY, EIA_API_KEY, FINNHUB_API_KEY).
          </p>
        </details>
        """

    return greeks_html + breaker_html + empty_html + new_factors_html + engines_html


_INTERACTIVE_JS = r"""<script>
(() => {
  const allCards = Array.from(document.querySelectorAll('article.card'));
  const tableRows = Array.from(document.querySelectorAll('table.ranked tbody tr, table.positions-table tbody tr'));
  const searchBox = document.getElementById('search-box');
  const sortBy = document.getElementById('sort-by');
  const chips = Array.from(document.querySelectorAll('.filter-chip'));
  const counter = document.getElementById('card-counter');
  const lookupPanel = document.getElementById('lookup-panel');
  const lookupHeading = document.getElementById('lookup-heading');
  const lookupSubtitle = document.getElementById('lookup-subtitle');
  const lookupResults = document.getElementById('lookup-results');
  const densityToggle = document.getElementById('density-toggle');
  const topOnly = document.getElementById('top-only');
  const expandAll = document.getElementById('expand-all');
  const collapseAll = document.getElementById('collapse-all');
  const resetFilters = document.getElementById('reset-filters');
  const downloadCsv = document.getElementById('download-csv');
  const downloadJson = document.getElementById('download-json');
  const copyVisible = document.getElementById('copy-visible');
  const printDashboard = document.getElementById('print-dashboard');
  const toastEl = document.getElementById('dashboard-toast');
  let activeFilter = 'all';
  let limitTop = false;

  try {
    if (localStorage.getItem('optedge_compact') === '1') {
      document.body.classList.add('compact');
      if (densityToggle) densityToggle.textContent = 'Comfortable';
    }
  } catch (e) {}

  function toast(message) {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.classList.add('active');
    clearTimeout(toastEl._timer);
    toastEl._timer = setTimeout(() => toastEl.classList.remove('active'), 1800);
  }

  function num(card, attr, def) {
    if (def === undefined) def = 0;
    const v = parseFloat(card.dataset[attr]);
    return isFinite(v) ? v : def;
  }

  function cardLabel(card) {
    const ticker = (card.dataset.ticker || '').toUpperCase();
    const side = card.dataset.side || 'idea';
    const status = (card.dataset.status || 'watch').toLowerCase();
    const conf = num(card, 'conf', null);
    const confText = conf === null ? '' : ` conf ${Math.round(conf)}`;
    return { ticker, side, status, confText };
  }

  function visibleCards() {
    return allCards.filter(card => !card.classList.contains('hidden'));
  }

  function cardRecord(card) {
    const ticker = (card.dataset.ticker || '').toUpperCase();
    const contract = (card.querySelector('.contract-line')?.innerText || '').trim();
    const title = (card.querySelector('.ticker')?.innerText || ticker).trim();
    return {
      ticker,
      title,
      asset: card.dataset.side || '',
      status: card.dataset.status || '',
      confidence: num(card, 'conf', 0),
      predicted_return_pct: num(card, 'pred', 0),
      ev_pct: num(card, 'ev', 0),
      kelly_pct: num(card, 'kelly', 0),
      dte: num(card, 'dte', 0),
      contract,
      text: (card.innerText || '').replace(/\s+/g, ' ').trim()
    };
  }

  function downloadText(filename, mime, text) {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function csvEscape(value) {
    const text = String(value ?? '');
    return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }

  function exportRows(format) {
    const rows = visibleCards().map(cardRecord);
    if (!rows.length) {
      toast('No visible cards to export.');
      return;
    }
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    if (format === 'json') {
      downloadText(`optedge-visible-${stamp}.json`, 'application/json', JSON.stringify(rows, null, 2));
      toast(`Downloaded ${rows.length} visible ideas as JSON.`);
      return;
    }
    const columns = ['ticker','asset','status','confidence','predicted_return_pct','ev_pct','kelly_pct','dte','contract','title'];
    const csv = [columns.join(',')]
      .concat(rows.map(row => columns.map(col => csvEscape(row[col])).join(',')))
      .join('\n');
    downloadText(`optedge-visible-${stamp}.csv`, 'text/csv', csv);
    toast(`Downloaded ${rows.length} visible ideas as CSV.`);
  }

  function updateLookup(q, visibleCards, visibleRows) {
    if (!lookupPanel || !lookupResults) return;
    if (!q) {
      lookupPanel.classList.remove('active');
      lookupResults.innerHTML = '';
      return;
    }
    const cardHits = visibleCards.slice(0, 10).map(card => {
      const meta = cardLabel(card);
      return `<button class="lookup-hit" type="button" data-jump="${meta.ticker}"><b>${meta.ticker}</b><span>${meta.side} / ${meta.status}${meta.confText}</span></button>`;
    });
    const rowHits = visibleRows.slice(0, Math.max(0, 10 - cardHits.length)).map(row => {
      const label = (row.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 90);
      return `<span class="lookup-hit"><b>row</b><span>${label}</span></span>`;
    });
    lookupHeading.textContent = `Lookup: ${q}`;
    lookupSubtitle.textContent = `${visibleCards.length} cards and ${visibleRows.length} table rows matched this scan`;
    lookupResults.innerHTML = cardHits.concat(rowHits).join('') || '<span class="muted">No matches in this generated dashboard. Run a fresh scan if this ticker is not in the current universe.</span>';
    lookupPanel.classList.add('active');
    lookupResults.querySelectorAll('[data-jump]').forEach(btn => {
      btn.addEventListener('click', () => {
        const ticker = btn.dataset.jump;
        const card = allCards.find(c => (c.dataset.ticker || '').toUpperCase() === ticker && !c.classList.contains('hidden'));
        if (!card) return;
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        card.animate([{ outlineColor: '#3b82f6' }, { outlineColor: 'transparent' }], { duration: 900 });
      });
    });
  }

  function applyFilters() {
    const q = (searchBox.value || '').trim().toUpperCase();
    let visible = 0;
    let filtered = 0;
    const visibleCards = [];
    allCards.forEach(card => {
      const ticker = (card.dataset.ticker || '').toUpperCase();
      const side = card.dataset.side;
      const status = (card.dataset.status || '').toLowerCase();
      const conf = num(card, 'conf');
      const ev = num(card, 'ev');
      const kelly = num(card, 'kelly');

      let show = true;
      if (q && !ticker.includes(q)) show = false;
      if (activeFilter === 'call' && side !== 'call') show = false;
      if (activeFilter === 'put' && side !== 'put') show = false;
      if (activeFilter === 'shares' && side !== 'shares') show = false;
      if (activeFilter === 'value' && side !== 'value') show = false;
      if (activeFilter === 'futures' && side !== 'futures') show = false;
      if (activeFilter === 'ready' && status !== 'trade') show = false;
      if (activeFilter === 'watch' && status !== 'watch') show = false;
      if (activeFilter === 'high-conf' && conf < 70) show = false;
      if (activeFilter === 'positive-ev' && ev <= 0) show = false;
      if (activeFilter === 'positive-kelly' && kelly <= 0) show = false;

      if (show) {
        filtered++;
        if (limitTop && filtered > 10) show = false;
      }
      card.classList.toggle('hidden', !show);
      if (show) {
        visible++;
        visibleCards.push(card);
      }
    });
    const visibleRows = [];
    tableRows.forEach(row => {
      const text = (row.innerText || '').toUpperCase();
      const show = !q || text.includes(q);
      row.classList.toggle('hidden', !show);
      if (show && q) visibleRows.push(row);
    });
    counter.textContent = visible + ' card' + (visible !== 1 ? 's' : '') +
      ' / ' + (q ? visibleRows.length : tableRows.length) + ' row' +
      ((q ? visibleRows.length : tableRows.length) !== 1 ? 's' : '') + ' visible' +
      (limitTop && filtered > visible ? ` (${filtered - visible} hidden by Top 10)` : '');
    updateLookup(q, visibleCards, visibleRows);
  }

  function applySort() {
    const mode = sortBy.value;
    if (mode === 'default') return;
    const sectionGroups = new Map();
    allCards.forEach(card => {
      const parent = card.parentElement;
      if (!sectionGroups.has(parent)) sectionGroups.set(parent, []);
      sectionGroups.get(parent).push(card);
    });
    sectionGroups.forEach((cards, parent) => {
      cards.sort((a, b) => {
        if (mode === 'ticker') {
          return (a.dataset.ticker || '').localeCompare(b.dataset.ticker || '');
        }
        const key = mode === 'pred' ? 'pred' :
                    mode === 'ev' ? 'ev' :
                    mode === 'kelly' ? 'kelly' : 'conf';
        return num(b, key) - num(a, key);
      });
      cards.forEach(c => parent.appendChild(c));
    });
  }

  searchBox.addEventListener('input', applyFilters);
  sortBy.addEventListener('change', () => { applySort(); applyFilters(); });
  chips.forEach(chip => {
    chip.addEventListener('click', () => {
      chips.forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      activeFilter = chip.dataset.filter;
      applyFilters();
    });
  });

  densityToggle.addEventListener('click', () => {
    document.body.classList.toggle('compact');
    densityToggle.textContent = document.body.classList.contains('compact') ? 'Comfortable' : 'Compact';
    try { localStorage.setItem('optedge_compact', document.body.classList.contains('compact') ? '1' : '0'); } catch (e) {}
  });
  topOnly.addEventListener('click', () => {
    limitTop = !limitTop;
    topOnly.classList.toggle('active', limitTop);
    topOnly.textContent = limitTop ? 'All visible' : 'Top 10';
    applyFilters();
  });
  expandAll.addEventListener('click', () => {
    document.querySelectorAll('details.dash-section').forEach(d => d.setAttribute('open', ''));
  });
  collapseAll.addEventListener('click', () => {
    document.querySelectorAll('details.dash-section').forEach(d => {
      if (d.id !== 'sect-analytics') d.removeAttribute('open');
    });
  });
  resetFilters.addEventListener('click', () => {
    searchBox.value = '';
    sortBy.value = 'default';
    activeFilter = 'all';
    limitTop = false;
    topOnly.classList.remove('active');
    topOnly.textContent = 'Top 10';
    chips.forEach(c => c.classList.toggle('active', c.dataset.filter === 'all'));
    applyFilters();
  });
  downloadCsv.addEventListener('click', () => exportRows('csv'));
  downloadJson.addEventListener('click', () => exportRows('json'));
  copyVisible.addEventListener('click', async () => {
    const tickers = Array.from(new Set(visibleCards().map(c => (c.dataset.ticker || '').toUpperCase()).filter(Boolean)));
    if (!tickers.length) {
      toast('No visible tickers to copy.');
      return;
    }
    const text = tickers.join('\n');
    try {
      await navigator.clipboard.writeText(text);
      toast(`Copied ${tickers.length} tickers.`);
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      toast(`Copied ${tickers.length} tickers.`);
    }
  });
  printDashboard.addEventListener('click', () => window.print());
  window.addEventListener('keydown', (event) => {
    if (event.key === '/' && document.activeElement !== searchBox) {
      event.preventDefault();
      searchBox.focus();
      searchBox.select();
    }
    if (event.key === 'Escape' && document.activeElement === searchBox) {
      searchBox.value = '';
      applyFilters();
      searchBox.blur();
    }
  });

  applyFilters();
})();

// Collapsible sections - persist state in localStorage
(() => {
  const SECT_KEY = 'optedge_section_state_v1';
  let state = {};
  try { state = JSON.parse(localStorage.getItem(SECT_KEY) || '{}'); } catch (e) {}
  document.querySelectorAll('details.dash-section').forEach(d => {
    const id = d.id;
    if (id && id in state) {
      if (state[id]) d.setAttribute('open', '');
      else d.removeAttribute('open');
    }
    d.addEventListener('toggle', () => {
      if (!d.id) return;
      state[d.id] = d.open;
      try { localStorage.setItem(SECT_KEY, JSON.stringify(state)); } catch (e) {}
    });
  });
})();
</script>"""

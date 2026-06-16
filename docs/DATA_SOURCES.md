# Data Sources

Optedge favors free or locally available data sources and degrades gracefully when a source is unavailable.

## Core Sources

- Options chains through the layered chain provider: optional broker/live sources first, then free CBOE/Nasdaq, bounded Yahoo options JSON, and yfinance fallbacks.
- Market history through Yahoo chart data and `yfinance`, then public no-key Nasdaq historical JSON, then Stooq CSV as a final best-effort fallback.
- Symbol search/universe hygiene through the official no-key Nasdaq Trader symbol directory plus SEC company tickers.
- Small-cap mover discovery through Nasdaq's public stock screener endpoint, enriched with FINRA short-volume context when available, and surfaced as delayed review candidates in Swing Scout.
- Reddit and retail-attention signals from WSB, r/options, and related public endpoints.
- SEC data for insider transactions, recent filings, companyfacts fundamentals, Form 144, buybacks, and 13F-style institutional context.
- Public macro and market structure inputs such as keyless FRED CSV series, official Treasury XML yield-curve fallback, yield curve, credit spreads, CFTC CoT, FINRA short volume, Nasdaq Trader trade halts, Nasdaq Trader Reg SHO threshold securities, EIA, WASDE, VIX term structure, and sector ETF flows.
- Optional sentiment models through local/GPU-enabled FinBERT variants.

## Reliability

Source failures should not silently become bullish or bearish signals. Engines return empty data or neutral rows when they cannot collect enough evidence, and the research guard can warn when key engines fail.

Free public endpoints can be delayed, rate-limited, incomplete, or temporarily blocked. Optedge treats them as research inputs, not guaranteed live execution quotes.

## Local Files

Private keys and generated data stay local by default through `.gitignore`.

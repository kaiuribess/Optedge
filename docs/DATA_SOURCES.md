# Data Sources

Optedge favors free or locally available data sources and degrades gracefully when a source is unavailable.

## Core Sources

- Options chains and market history through configured providers and `yfinance`.
- Reddit and retail-attention signals from WSB, r/options, and related public endpoints.
- SEC data for insider transactions, Form 144, buybacks, and 13F-style institutional context.
- Public macro and market structure inputs such as yield curve, credit spreads, CFTC CoT, EIA, WASDE, VIX term structure, and sector ETF flows.
- Optional sentiment models through local/GPU-enabled FinBERT variants.

## Reliability

Source failures should not silently become bullish or bearish signals. Engines return empty data or neutral rows when they cannot collect enough evidence, and the research guard can warn when key engines fail.

## Local Files

Private keys and generated data stay local by default through `.gitignore`.

# Factor Library

Optedge combines multiple families of evidence:

- Mispricing: option fair-value differences, implied volatility, skew, CBOE theo comparison, model ensemble diagnostics.
- Sentiment: WSB, r/options, StockTwits-style retail attention, FinBERT-scored text.
- Fundamentals: valuation, quality, market cap, sector and classification context.
- Earnings: calendar proximity, whisper signals, IV-crush risk.
- Insider and filings: insider transactions, Form 144, buybacks, 13F context.
- Macro and futures: yield curve, credit spreads, EIA, WASDE, CoT, futures trend/range features.
- Market structure: unusual options activity, put/call, dark-pool and short-volume proxies, SEC fails-to-deliver context, sector ETF flows.
- Technicals: trend, momentum, relative strength, volatility regime.

Option pricing keeps two concepts separate. `net_edge_pct` is the absolute model-price anomaly after spread and is useful for diagnostics. `buyer_edge_pct` is directional: it is positive only when model value exceeds market mid by more than the estimated round-trip spread. New long-option Trade rows must clear the buyer edge gate; an overpriced contract can remain visible for review but cannot be promoted merely because its absolute anomaly is large.

Each factor should prove its value through forward testing, information coefficient tracking, and validation buckets before being trusted in sizing.

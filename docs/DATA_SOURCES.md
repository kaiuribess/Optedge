<!-- Purpose: Document market-data integrations, provenance, fallbacks, and reliability limits. -->

# Data Sources

Optedge favors free or locally configured research inputs. An integration listed here is a code path, not a promise that the provider is reachable, real-time, complete, or enabled in a particular run. Provider terms, entitlements, redistribution rights, schemas, and rate limits remain outside Optedge's control.

## Provenance Classes

| Class | Meaning | Safe interpretation |
|---|---|---|
| Direct provider observation | A value returned by the named upstream source. | Use only with its upstream timestamp, market session, and provider caveats. |
| Provider-derived proxy | A public series used as context for a concept it does not measure exactly. | Context only; do not relabel it as the target quantity. |
| Modeled fallback | A value estimated from observed inputs when direct history is unavailable. | Keep it separate from observed outcomes and verified fills. |
| Synthetic/demo | Locally generated data used to exercise the interface. | Never use it as market evidence or a performance claim. |
| Broker-cache observation | A timestamped read captured through a connected Robinhood/Codex session. | Read-only point-in-time evidence; not a fill, authorization, or continuously live feed. |

## Provider Matrix

| Research area | Integration paths | Qualification |
|---|---|---|
| Equity and ETF history | Yahoo chart data and `yfinance`, with public Nasdaq historical JSON and Stooq CSV fallbacks | Sources can differ on adjustments, timestamps, missing sessions, symbols, and corporate actions. A fallback response is not automatically equivalent to the primary series. |
| Futures and cross-asset history | Public market-history paths, including continuous or proxy series where configured | Continuous contracts and ETF proxies can diverge from a tradable contract month and its actual roll/slippage. |
| Option chains | Optional Tradier token, then free Cboe delayed data, Nasdaq, bounded Yahoo options JSON, and `yfinance` fallbacks | Coverage, Greeks, open interest, multiplier metadata, and quote timestamps vary. Free chains may be delayed or internally inconsistent and are not execution quotes. |
| Robinhood market research | Optional authenticated connector outside the local Python process, using bounded request/cache files for equities, exact options, and history | The upstream timestamp controls freshness. Exact option trade bars are market observations, not bid/ask fills and not proof Optedge traded. The research cache deliberately excludes accounts and orders. |
| Robinhood account state | Separate account-scoped read bundle normalized into a pseudonymous broker snapshot | Used only for readiness, reconciliation, and total-open exposure checks. It can become stale immediately and never authorizes an order by itself. |
| Corporate filings and fundamentals | SEC EDGAR submissions, companyfacts, Form 4, Form 144, recent filings, and related public filing records | SEC requests require a real operator email in `OPTEDGE_CONTACT` (or a real email embedded in legacy `SEC_USER_AGENT`) and fail before the request when none is configured. Filing data can be amended, delayed, issuer-specific, or difficult to map. A filing-derived score is research context, not a legal conclusion. |
| Macro, rates, and credit | Keyless FRED graph CSV series and official Treasury yield-curve fallback where configured | Series can be revised, published on different schedules, and transformed into proxies. Missing observations should remain unavailable rather than forward-filled without a label. |
| Commodities and positioning | Public CFTC Commitments of Traders, EIA energy, and USDA WASDE paths | Reports are periodic and delayed; they are not intraday positioning or executable prices. |
| Options-market context | Cboe daily market statistics, put/call series, and public option-symbol activity | Delayed aggregate activity is contextual. It does not identify buyer intent, guarantee unusual flow, or prove a directional trade. |
| Short and settlement context | Official FINRA short-volume and twice-monthly short-interest files plus SEC fails-to-deliver files | Short volume is not short interest. Fails to deliver are delayed settlement records, not proof of manipulation, future price direction, or a squeeze. |
| Market status and symbol hygiene | Nasdaq Trader symbol directory, trade halts, Reg SHO threshold securities, short-sale circuit breakers, plus SEC company tickers | Lists have different publication schedules and scopes. Symbol mapping can break around renames, classes, delistings, and corporate actions. |
| Small-cap discovery | Nasdaq public screener with delayed price/mover context | Discovery candidates require independent freshness, liquidity, filing, and risk checks before they can support a plan. |
| Social and news context | Public Reddit/retail-attention endpoints and configured news/earnings sources | Availability and terms change frequently. Social volume and sentiment are noisy, gameable, and not representative samples. |
| Text sentiment | VADER and optional local/GPU FinBERT models | Model scores are classifications with domain and language error; they are not facts or expected returns. |

## Fallback and Freshness Rules

- A newly written cache file does not make an old upstream quote fresh.
- Provider retrieval time, exchange timestamp, market session, and report publication date are different concepts and should remain distinguishable.
- A fallback should retain source/provenance fields when the downstream schema supports them.
- An engine failure should return empty, neutral, unavailable, or blocked output; it must not silently become bullish or bearish evidence.
- A modeled option outcome must remain labeled separately from a broker-market-observed bar, and neither is a verified Optedge fill.
- Demo output must remain visibly synthetic and cannot clear Edge Lab or a broker-review gate.

Run `python setup_check.py` to inspect basic local readiness. Passing that check means the configured path responded during the check; it does not guarantee future availability or data quality.

## Credentials and Provider Terms

Optional providers may require values in local environment variables or `keys.py`. Those paths are ignored by Git, but ignore rules are not encryption. Never commit or paste credentials into issues, screenshots, dashboards, or broker packets.

SEC access is a special case: set `OPTEDGE_CONTACT` to a real operator email you control. Placeholder, example, `.local`, `.test`, and other non-contact addresses are rejected. Legacy `SEC_USER_AGENT` remains compatible only when it contains a real email. Optedge includes that contact only in SEC request headers; general non-SEC requests use `Optedge/<version>` without the email. The contact is not a secret credential, but it is personal information and should not be committed or published in logs.

Users are responsible for complying with each provider's terms, licensing, attribution, and rate limits. Optedge does not grant redistribution rights to third-party data.

## Local Files

Generated research, logs, telemetry, model state, broker snapshots, and queues stay local by default under Git-ignored paths. Raw broker captures can contain full account identifiers; normalized snapshots use pseudonymous account keys but remain financially sensitive. See [Third-Party Forward Testing](THIRD_PARTY_FORWARD_TESTING.md) for the separate research-cache and account-snapshot boundaries.

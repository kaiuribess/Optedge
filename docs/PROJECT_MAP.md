<!-- Purpose: Explain every source-controlled repository path in one maintained index. -->

# Complete Project Map

This is the canonical one-line inventory of Optedge's source-controlled files.
It covers application code, research engines, risk controls, documentation,
configuration, examples, and regression tests. Private runtime data and other
ignored generated artifacts are intentionally absent.

GitHub's text beside a file is the latest commit subject that touched that
path; it is not a per-file description field. This map explains file purpose,
while commit subjects remain descriptions of complete logical changes.

`tests/test_project_map.py` checks that every Git-tracked path appears exactly
once here. When a file is added or renamed, update this map in the same change.

The large self-contained cockpit and its test suite remain explicit future
maintainability work: `scripts/local_cockpit.py` and
`tests/test_local_cockpit.py` should eventually be split by API, evidence,
broker-boundary, state, and UI responsibilities. This release does not claim
that refactor has already happened.

## Repository root

| Path | Purpose |
|---|---|
| `.gitattributes` | Normalizes text line endings and protects binary formats from conversion. |
| `.gitignore` | Keeps secrets, private runtime state, build products, and local tooling files out of Git. |
| `CODE_OF_CONDUCT.md` | Defines respectful participation and proportionate community enforcement expectations. |
| `CONTRIBUTING.md` | Defines the safe development, evidence, testing, and pull-request workflow. |
| `LICENSE` | Applies the MIT license to the project source. |
| `README.md` | Provides the primary product overview, setup, workflow, safety boundaries, and project layout. |
| `SECURITY.md` | Explains private vulnerability reporting and sensitive financial-data handling. |
| `archive.py` | Moves generated run artifacts into timestamped archives without deleting source code. |
| `async_http.py` | Provides bounded asynchronous HTTP fetching and shared provider-request helpers. |
| `chain_provider.py` | Fetches, normalizes, caches, and diagnoses multi-source option chains. |
| `config.py` | Defines shared universes, research defaults, limits, feature flags, and risk settings. |
| `data_provider.py` | Supplies cached and normalized market history, quotes, and provider status. |
| `demo_data.py` | Generates deterministic schema-compatible demo inputs that never represent live evidence. |
| `diagnose_gpu.py` | Diagnoses PyTorch, NVIDIA driver, and CUDA readiness for optional FinBERT acceleration. |
| `install.bat` | Creates and validates a local Optedge environment on Windows. |
| `install.sh` | Creates and validates a local Optedge environment on Linux or macOS. |
| `pricing_models.py` | Implements scalar, vectorized, and regime-aware option-pricing models. |
| `pyproject.toml` | Declares package metadata, dependencies, extras, entry points, build rules, and test tools. |
| `requirements.txt` | Installs this checkout in editable mode using `pyproject.toml` as the dependency source. |
| `run.bat` | Launches the source checkout on Windows and prefers its local virtual environment. |
| `run.py` | Starts Optedge from source by delegating command routing to `optedge.cli`. |
| `setup_check.py` | Checks Python and provider readiness and saves a local health summary. |
| `universe_filter.py` | Selects a liquid, relevant ticker subset before expensive research engines run. |
| `utils.py` | Provides shared retry, option-math, statistics, parsing, time, and numeric helpers. |

## `.github/`

| Path | Purpose |
|---|---|
| `.github/AUTOMATION.md` | Explains continuous integration, dependency maintenance, and workflow permissions. |
| `.github/ISSUE_TEMPLATE/bug_report.yml` | Collects reproducible bug reports without soliciting private financial data. |
| `.github/ISSUE_TEMPLATE/config.yml` | Routes public issues, private security reports, and documentation questions. |
| `.github/ISSUE_TEMPLATE/feature_request.yml` | Collects evidence-aware feature proposals within the project's safety boundaries. |
| `.github/PULL_REQUEST_TEMPLATE.md` | Captures validation, privacy, broker-boundary, and release context for pull requests. |
| `.github/dependabot.yml` | Configures weekly Python and GitHub Actions dependency update proposals. |
| `.github/workflows/ci.yml` | Tests supported Python versions and enforces the focused release lint gate. |

## `backtest/`

| Path | Purpose |
|---|---|
| `backtest/README.md` | Explains validation, position lifecycle, sizing, and evidence responsibilities. |
| `backtest/__init__.py` | Marks and describes the backtesting and lifecycle package. |
| `backtest/alpha_decay.py` | Preserves a labeled diagnostic-only exploratory factor-decay API for compatibility. |
| `backtest/calibration.py` | Measures predicted-versus-realized calibration separately for each asset class. |
| `backtest/drawdown_breaker.py` | Computes research drawdown state and its risk-reduction multiplier. |
| `backtest/edge_lab.py` | Produces fail-closed asset evidence with provenance, spread-cost, stability, and live-review gates. |
| `backtest/exit_learning.py` | Learns conservative exit-policy adjustments from eligible lifecycle evidence. |
| `backtest/exit_rules.py` | Applies shared dynamic exit-review logic to options, shares, and futures. |
| `backtest/fixed_horizon.py` | Settles leakage-resistant, profile-isolated fixed-session outcomes with model provenance and spread-aware option costs. |
| `backtest/forward.py` | Reprices logged signals as mixed-age current-mark monitoring telemetry. |
| `backtest/futures_positions.py` | Tracks simulated futures positions through their research lifecycle. |
| `backtest/futures_sizing.py` | Sizes futures risk with contract multipliers and micro-contract preference. |
| `backtest/historical.py` | Runs a quarantined look-ahead factor diagnostic that cannot prove edge. |
| `backtest/leaps_edge.py` | Evaluates the dedicated LEAPS swing evidence lane across its required 5-, 10-, and 20-session horizons. |
| `backtest/model_accuracy.py` | Reports quarantined current-mid pricing-model diagnostics without promoting weights. |
| `backtest/option_expiry.py` | Values and records option-expiry outcomes with auditable assumptions. |
| `backtest/option_history.py` | Manages the read-only Robinhood option-history cache and request queue. |
| `backtest/portfolio_greeks.py` | Aggregates option Greeks and concentration across the research portfolio. |
| `backtest/positions.py` | Tracks simulated option position marks, exits, and realized research P&L. |
| `backtest/predictor.py` | Quarantines research fits and loads only digest-valid, purged-OOS predictor and weight champions. |
| `backtest/share_positions.py` | Tracks simulated share positions through their research lifecycle. |
| `backtest/sizing.py` | Calculates cost-aware expected value, Kelly sizing, and concentration limits. |
| `backtest/track.py` | Writes per-asset signal logs with timestamps, execution profiles, holding intent, and evidence provenance. |

## `dashboard/`

| Path | Purpose |
|---|---|
| `dashboard/README.md` | Explains the generated standalone market-research dashboard. |
| `dashboard/__init__.py` | Marks and describes the dashboard-rendering package. |
| `dashboard/build.py` | Renders ranked research, lifecycle, risk, and telemetry data into self-contained HTML. |

## `data/`

| Path | Purpose |
|---|---|
| `data/.keep` | Keeps the private runtime-data directory present in clean clones. |
| `data/README.md` | Explains private runtime research state, broker snapshots, and why the durable real-account equity ledger defaults to per-user OS state outside the checkout. |
| `data/weights/.keep` | Keeps the ignored learned-weight directory present in clean clones. |

## `docs/`

| Path | Purpose |
|---|---|
| `docs/ARCHITECTURE.md` | Explains system flow, module boundaries, asset lifecycles, and broker separation. |
| `docs/DATA_SOURCES.md` | Documents market-data providers, provenance, fallbacks, freshness, and reliability. |
| `docs/EDGE_LAB.md` | Defines Edge Lab evidence lanes, calculations, thresholds, and limitations. |
| `docs/FACTOR_LIBRARY.md` | Catalogs each research factor and the information it contributes. |
| `docs/FREE_DATA_ROADMAP.md` | Tracks planned improvements to the no-subscription market-data stack. |
| `docs/LEAPS_SWING.md` | Defines the profile-isolated LEAPS contract, liquidity, evidence, holding, risk, and broker-review policy. |
| `docs/LIMITATIONS.md` | States statistical, data, execution, and operational limitations. |
| `docs/PROJECT_MAP.md` | Provides this canonical one-line purpose description for every repository path. |
| `docs/README.md` | Indexes the durable architecture, evidence, risk, data, and operating guides. |
| `docs/RISK_MODEL.md` | Defines sizing, exposure, drawdown, and manual-review guardrails. |
| `docs/THIRD_PARTY_FORWARD_TESTING.md` | Defines external paper tracking, direct Robinhood sync, manual capture fallback, broker-preview limits, and verification workflows. |
| `docs/VALIDATION.md` | Defines eligible evidence, exclusions, sample rules, and validation artifacts. |

## `engines/`

| Path | Purpose |
|---|---|
| `engines/README.md` | Explains the independent market-research engine layer and failure behavior. |
| `engines/__init__.py` | Marks and describes the market-research engine package. |
| `engines/analyst.py` | Collects and scores analyst recommendation context. |
| `engines/backtest_engine.py` | Preserves a labeled legacy chronological factor diagnostic for compatibility. |
| `engines/buybacks.py` | Detects SEC 8-K share-repurchase announcements. |
| `engines/cboe_symbol_data.py` | Collects public Cboe option-symbol activity and quote context. |
| `engines/cluster_buys.py` | Detects clusters of related Form 4 insider purchases. |
| `engines/congress.py` | Collects House and Senate transaction-disclosure research. |
| `engines/cot.py` | Scores CFTC Commitments of Traders positioning. |
| `engines/credit_spread.py` | Measures investment-grade and high-yield credit-spread conditions. |
| `engines/dark_pool.py` | Measures FINRA off-exchange and short-volume context. |
| `engines/diagnose.py` | Prints a read-only diagnostic of current logs, evidence, Edge Lab, and weights. |
| `engines/earnings.py` | Collects upcoming earnings-calendar context. |
| `engines/eia.py` | Measures EIA petroleum and natural-gas inventory conditions. |
| `engines/fda_calendar.py` | Collects layered FDA and biotech catalyst-calendar context. |
| `engines/finbert.py` | Optionally classifies financial text sentiment with local FinBERT models. |
| `engines/finnhub_provider.py` | Shares bounded Finnhub access across analyst, insider, and catalyst engines. |
| `engines/form_144.py` | Collects SEC Form 144 proposed-sale notices. |
| `engines/forward_test.py` | Preserves a labeled no-promotion legacy forward-replay API for compatibility. |
| `engines/fred_public.py` | Fetches keyless public FRED CSV series with bounded caching. |
| `engines/fundamentals.py` | Collects and scores company fundamental data. |
| `engines/futures.py` | Researches equity-index, commodity, rate, currency, and crypto futures. |
| `engines/google_trends.py` | Measures search interest using Google Trends and Wikipedia pageviews. |
| `engines/hyperliquid.py` | Measures Hyperliquid perpetual-futures open interest. |
| `engines/insider.py` | Collects and scores SEC Form 4 insider activity. |
| `engines/iv_surface.py` | Detects implied-volatility surface anomalies across nearby strikes. |
| `engines/learning.py` | Manages versioned per-bucket factor priors and eligible learned weights. |
| `engines/macro.py` | Builds macroeconomic and market-regime context. |
| `engines/mispricing.py` | Compares option market prices with the pricing-model ensemble. |
| `engines/nasdaq_screener.py` | Provides public Nasdaq stock-universe and symbol helpers. |
| `engines/news.py` | Collects free RSS headlines and scores news sentiment. |
| `engines/nitter.py` | Measures Twitter-style retail attention through available public sources. |
| `engines/put_call.py` | Derives put-call activity ratios from collected option chains. |
| `engines/r_options.py` | Measures discussion and sentiment from the public r/options community. |
| `engines/regsho_threshold.py` | Monitors the Nasdaq Trader Reg SHO threshold-security list. |
| `engines/sec_ftd.py` | Collects SEC fails-to-deliver context. |
| `engines/sector_etf_flow.py` | Estimates sector ETF flow and institutional rotation context. |
| `engines/sector_rs.py` | Measures ticker relative strength against its sector. |
| `engines/sentiment.py` | Scores public Reddit and retail-discussion sentiment. |
| `engines/short_interest.py` | Measures short-interest and squeeze-setup context. |
| `engines/short_sale_circuit.py` | Monitors Nasdaq short-sale circuit-breaker status. |
| `engines/social.py` | Collects non-Reddit public social and attention signals. |
| `engines/technicals.py` | Computes price, trend, momentum, volatility, and range indicators. |
| `engines/thirteen_f.py` | Measures institutional holdings changes from SEC 13F filings. |
| `engines/trading_halts.py` | Monitors the Nasdaq Trader trade-halt feed. |
| `engines/uoa.py` | Derives unusual option-activity signals from collected chains. |
| `engines/value.py` | Scores valuation and quality with an enterprise-value framework. |
| `engines/vix_term.py` | Measures VIX futures term structure and volatility regime. |
| `engines/wasde.py` | Collects USDA WASDE agriculture-supply context. |
| `engines/whisper.py` | Builds layered earnings-catalyst and expectation context. |
| `engines/wsb_trending.py` | Discovers tickers trending in public WallStreetBets discussion. |
| `engines/yield_curve_pca.py` | Extracts yield-curve level, slope, and curvature factors. |

## `examples/`

| Path | Purpose |
|---|---|
| `examples/README.md` | Explains sanitized examples and their separation from live evidence. |
| `examples/validation_summary.example.json` | Provides a sanitized validation-summary example for schema and documentation checks. |

## `fusion/`

| Path | Purpose |
|---|---|
| `fusion/README.md` | Explains signal normalization, attribution, and ranking. |
| `fusion/__init__.py` | Marks and describes the signal-fusion package. |
| `fusion/attribution.py` | Identifies the strongest factor contributions for each ranked idea. |
| `fusion/rank.py` | Merges engine evidence into ranked options, shares, value, and futures research. |

## `logs/`

| Path | Purpose |
|---|---|
| `logs/.keep` | Keeps the private local signal-history directory present in clean clones. |
| `logs/README.md` | Explains private scan histories and their validation role. |

## `optedge/`

| Path | Purpose |
|---|---|
| `optedge/README.md` | Explains the installable application and central orchestration package. |
| `optedge/__init__.py` | Exposes package identity and the canonical application version. |
| `optedge/__main__.py` | Supports launching the installed application with `python -m optedge`. |
| `optedge/cli.py` | Routes command-line requests to scans, loops, lookup, validation, or the cockpit. |
| `optedge/default_weights/futures_agri.json` | Ships immutable fallback weights for the agriculture-futures bucket. |
| `optedge/default_weights/futures_crypto.json` | Ships immutable fallback weights for the crypto-futures bucket. |
| `optedge/default_weights/futures_currency.json` | Ships immutable fallback weights for the currency-futures bucket. |
| `optedge/default_weights/futures_energy.json` | Ships immutable fallback weights for the energy-futures bucket. |
| `optedge/default_weights/futures_equity.json` | Ships immutable fallback weights for the equity-index-futures bucket. |
| `optedge/default_weights/futures_metal.json` | Ships immutable fallback weights for the metals-futures bucket. |
| `optedge/default_weights/futures_treasury.json` | Ships immutable fallback weights for the Treasury-futures bucket. |
| `optedge/default_weights/options_call.json` | Ships immutable fallback weights for long-call research. |
| `optedge/default_weights/options_put.json` | Ships immutable fallback weights for long-put research. |
| `optedge/default_weights/shares_long.json` | Ships immutable fallback weights for long-share research. |
| `optedge/engine_registry.py` | Preserves informational engine-name metadata without claiming dispatch authority. |
| `optedge/evidence_capture.py` | Freezes one explicitly checked Robinhood finalist into append-only, source-bound paper evidence without placing an order. |
| `optedge/http_identity.py` | Builds honest versioned HTTP identities and SEC-specific contact headers. |
| `optedge/leaps_swing.py` | Scores long-option candidates against the canonical LEAPS swing contract, liquidity, quote, edge, and budget gates. |
| `optedge/modes/__init__.py` | Marks the small command-mode wrapper package. |
| `optedge/modes/backtest.py` | Routes the historical diagnostic command mode. |
| `optedge/modes/forward.py` | Routes the current forward-telemetry command mode. |
| `optedge/modes/loop.py` | Runs repeated local research scans at an operator-selected interval. |
| `optedge/modes/scan.py` | Runs one local research scan. |
| `optedge/orchestrator.py` | Coordinates engines, fusion, risk controls, tracking, reports, and outputs. |
| `optedge/robinhood_connection.py` | Bridges the synchronous cockpit to one bounded private asyncio lifecycle with fixed confirmed-option placement and no generic dispatcher, polling, or retries. |
| `optedge/robinhood_finalist.py` | Resolves up to ten unchanged ranked option candidates against exact, short-lived Robinhood chain, contract, quote, price-cap, and liquidity evidence without broker writes. |
| `optedge/robinhood_mcp.py` | Implements official Robinhood MCP OAuth, OS-keyring credential storage, allowlisted reads and previews, plus one fixed confirmed-option placement boundary with no generic dispatcher. |
| `optedge/robinhood_option_execution.py` | Provides a two-click, single-use Robinhood option preview and placement boundary with final live revalidation and no automatic retry. |
| `optedge/robinhood_option_history_sync.py` | Collects a bounded batch of exact-contract Robinhood option histories through read-only MCP calls and atomically updates the validation cache. |
| `optedge/robinhood_snapshot_sync.py` | Performs one explicit complete account read, proves bounded pagination, and persists only a redacted broker snapshot and pseudonymous risk ledger. |
| `optedge/strategy_profile.py` | Defines canonical discovery, ordinary swing-execution, and profile-isolated LEAPS swing policies. |

## `reports/`

| Path | Purpose |
|---|---|
| `reports/README.md` | Explains validation and pricing-stability report generation. |
| `reports/__init__.py` | Marks and describes the report-generation package. |
| `reports/heston_stability.py` | Tests Heston pricing stability before the model is considered usable. |
| `reports/validation_report.py` | Builds formal local validation summaries, charts, JSON, and HTML reports. |

## `risk/`

| Path | Purpose |
|---|---|
| `risk/README.md` | Explains research, portfolio, and manual trade-plan guardrails. |
| `risk/__init__.py` | Marks and describes the risk-control package. |
| `risk/account_drawdown.py` | Validates policy-v2 Robinhood equity chains, 18-hour/two-New-York-date baselines, freshness, and fail-closed loss-based risk multipliers. |
| `risk/portfolio.py` | Calculates fail-closed same-account exposure and post-trade allocation headroom. |
| `risk/research_guard.py` | Blocks recommendations when evidence, drawdown, spread, freshness, or health is unsafe. |
| `risk/trade_plan.py` | Enforces profile-specific sizing, evidence, candidate, broker-state, and contract constraints while constructing short-lived Robinhood review packets. |

## `scripts/`

| Path | Purpose |
|---|---|
| `scripts/README.md` | Explains user-facing cockpit utilities and safe research handoffs. |
| `scripts/auto_agentic_paper.py` | Moves eligible queue ideas into a local paper book only, never a broker account. |
| `scripts/export_external_paper_track.py` | Exports a bounded research subset for third-party paper tracking. |
| `scripts/export_robinhood_agentic_queue.py` | Builds explicitly profile-isolated ordinary-swing or LEAPS option research queues without placing broker orders. |
| `scripts/local_cockpit.py` | Serves the loopback Trade Desk, profile-aware option/share candidate comparison, local planner, evidence firewalls, and fail-closed Robinhood review UI. |
| `scripts/lookup_symbol.py` | Builds a focused, source-backed research report for one ticker. |
| `scripts/normalize_robinhood_broker_snapshot.py` | Redacts and atomically persists normalized read-only Robinhood state plus durable pseudonymous equity ledgers and rollback-detection backups. |
| `scripts/refresh_robinhood_option_history.py` | Refreshes the read-only option-history bridge from explicit local inputs. |
| `scripts/research_jobs.py` | Runs and records bounded focused research jobs for the local cockpit. |
| `scripts/robinhood_research_bridge.py` | Reads cached Robinhood research context without credentials or broker writes. |
| `scripts/sec_filings.py` | Looks up recent SEC EDGAR filings with honest request identity. |
| `scripts/symbol_resolver.py` | Resolves ticker, company, and contract search text using free sources. |

## `telemetry/`

| Path | Purpose |
|---|---|
| `telemetry/README.md` | Explains engine health, cache, performance, and reliability telemetry. |
| `telemetry/__init__.py` | Marks and describes the telemetry package. |
| `telemetry/cache_stats.py` | Records cache hits, misses, and source-prefix summaries. |
| `telemetry/engine_health.py` | Maintains rolling per-engine availability and latency health. |
| `telemetry/health.py` | Records process memory, runtime errors, and cache growth across loop iterations. |
| `telemetry/perf.py` | Measures per-engine runtime and row-count performance. |

## `tests/`

| Path | Purpose |
|---|---|
| `tests/README.md` | Explains regression, safety, provider, dashboard, and broker-boundary coverage. |
| `tests/test_account_drawdown.py` | Protects equity hash chains, durable state paths/backups, baseline age/date requirements, freshness, drawdown thresholds, and fail-closed review behavior. |
| `tests/test_archive.py` | Protects non-destructive artifact archiving behavior. |
| `tests/test_auto_agentic_paper.py` | Protects local paper-book eligibility and lifecycle behavior. |
| `tests/test_calibration.py` | Protects strict asset-isolated calibration calculations and gates. |
| `tests/test_cboe_symbol_data.py` | Protects Cboe symbol-data parsing, caching, and failure handling. |
| `tests/test_chain_provider_tradier.py` | Protects Tradier and free option-chain fallback behavior. |
| `tests/test_cli.py` | Protects command routing and source versus installed entry points. |
| `tests/test_dashboard_data.py` | Protects standalone dashboard payload and rendering behavior. |
| `tests/test_data_provider_stooq.py` | Protects Stooq history fallback normalization. |
| `tests/test_drawdown_breaker.py` | Protects validated research-drawdown fallback and risk-reduction behavior. |
| `tests/test_edge_lab.py` | Protects Edge Lab schema, provenance, statistics, and fail-closed eligibility. |
| `tests/test_evidence_capture.py` | Protects source-bound manual evidence capture, profile isolation, append-only audits, and fail-closed behavior. |
| `tests/test_evidence_quarantine.py` | Ensures look-ahead diagnostics cannot promote production models. |
| `tests/test_examples.py` | Protects sanitized example schemas and privacy boundaries. |
| `tests/test_exit_learning.py` | Protects conservative exit-policy learning behavior. |
| `tests/test_exit_rules.py` | Protects cross-asset exit-review rules. |
| `tests/test_external_paper_track.py` | Protects bounded external paper-tracking exports. |
| `tests/test_finbert_batching.py` | Protects optional FinBERT batching and fallback behavior. |
| `tests/test_fixed_horizon.py` | Protects fixed-session settlement, costs, provenance, and coverage accounting. |
| `tests/test_fred_public.py` | Protects public FRED CSV fetching and caching. |
| `tests/test_futures_positions.py` | Protects simulated futures lifecycle accounting. |
| `tests/test_futures_sizing.py` | Protects multiplier-aware futures sizing. |
| `tests/test_http_identity.py` | Protects provider identity privacy and honest SEC contact enforcement. |
| `tests/test_leaps_edge.py` | Protects strict LEAPS profile isolation and all required fixed-horizon evidence gates. |
| `tests/test_leaps_swing.py` | Protects LEAPS candidate contract, liquidity, quote, edge, budget, and status scoring. |
| `tests/test_learning_weights.py` | Protects default and learned per-bucket weight behavior. |
| `tests/test_local_cockpit.py` | Protects Trade Desk APIs, profile-aware UI payloads, exact share/option attestations, planner, reconciliation, and broker boundaries. |
| `tests/test_lookup_symbol.py` | Protects focused ticker-research reports and provider degradation. |
| `tests/test_nasdaq_screener.py` | Protects Nasdaq screener parsing and symbol normalization. |
| `tests/test_news.py` | Protects RSS news collection, caching, and sentiment output. |
| `tests/test_option_expiry.py` | Protects option-expiry valuation and audit metadata. |
| `tests/test_option_history.py` | Protects read-only option-history coverage and request generation. |
| `tests/test_option_positions.py` | Protects simulated option position lifecycle accounting. |
| `tests/test_orchestrator_hygiene.py` | Protects orchestration boundaries and source-code hygiene invariants. |
| `tests/test_performance_cache.py` | Protects performance instrumentation and cache summaries. |
| `tests/test_portfolio.py` | Protects same-account exposure normalization and post-trade caps. |
| `tests/test_predictor_guard.py` | Protects predictor training eligibility and runtime-weight safeguards. |
| `tests/test_pricing.py` | Protects option pricing and cost-aware Kelly calculations. |
| `tests/test_project_map.py` | Ensures every Git-tracked path is described and legacy diagnostics stay fail-closed. |
| `tests/test_regsho_threshold.py` | Protects Reg SHO threshold-list parsing and caching. |
| `tests/test_research_guard.py` | Protects evidence, spread, drawdown, freshness, and engine-health blockers. |
| `tests/test_research_jobs.py` | Protects bounded background research-job state. |
| `tests/test_robinhood_agentic_queue.py` | Protects explicit ordinary-swing and LEAPS queue identity, geometry, evidence metadata, and eligibility. |
| `tests/test_robinhood_broker_snapshot.py` | Protects account-scoped broker snapshot redaction, atomic persistence, ledger integrity, and fail-closed parsing. |
| `tests/test_robinhood_connection.py` | Protects the bounded single-loop Robinhood connection lifecycle, sanitized status, OAuth callback, and tool-call boundaries. |
| `tests/test_robinhood_finalist.py` | Protects exact ranked-candidate identity, the ten-row cap, bounded contract resolution, live quote gates, source-digest binding, and the no-order research boundary. |
| `tests/test_robinhood_mcp.py` | Protects official-endpoint OAuth, keyring-only credential storage, schema checks, redaction, read/review separation, and fixed confirmed-option placement policy. |
| `tests/test_robinhood_option_execution.py` | Protects Robinhood option preview, confirmation-token, placement, redaction, and fail-closed execution behavior. |
| `tests/test_robinhood_option_history_sync.py` | Protects bounded exact-contract history collection, read-only tool use, and atomic all-or-nothing cache updates. |
| `tests/test_robinhood_research_bridge.py` | Protects read-only Robinhood research-cache behavior. |
| `tests/test_robinhood_snapshot_sync.py` | Protects explicit complete account reads, cursor proofs, option-instrument joins, and redacted-only direct snapshot persistence. |
| `tests/test_sec_companyfacts.py` | Protects SEC company-facts parsing and honest request use. |
| `tests/test_sec_ftd.py` | Protects SEC fails-to-deliver parsing and caching. |
| `tests/test_setup_check.py` | Protects environment and provider-readiness diagnostics. |
| `tests/test_share_positions.py` | Protects simulated share position lifecycle accounting. |
| `tests/test_short_interest.py` | Protects short-interest source parsing and scoring. |
| `tests/test_short_sale_circuit.py` | Protects short-sale circuit-breaker parsing and caching. |
| `tests/test_strategy_profile.py` | Protects canonical discovery, ordinary swing, and LEAPS swing policy constants and asset rules. |
| `tests/test_symbol_resolver.py` | Protects company, ticker, and option-contract resolution. |
| `tests/test_trade_plan.py` | Protects profile-specific sizing, evidence and candidate attestations, portfolio gates, state rechecks, and review packets. |
| `tests/test_trading_halts.py` | Protects trade-halt feed parsing and caching. |
| `tests/test_treasury_yield_curve.py` | Protects Treasury curve retrieval and factor calculations. |
| `tests/test_validation_report.py` | Protects validation metrics, artifacts, and no-data reporting. |

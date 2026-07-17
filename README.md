<!-- Purpose: Explain Optedge features, setup, safety boundaries, and repository layout. -->

# Optedge

### A free, local-first swing-trading research workstation

![CI](https://github.com/kaiuribess/Optedge/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.11--3.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-research-orange)

Optedge turns free and locally configured market data into a decision-first workspace for options, shares, futures, and value ideas. It combines contract analytics, technicals, fundamentals, filings, catalysts, macro context, sizing, lifecycle tracking, validation, and broker-readiness checks. The direct Robinhood connection never collects a password, MFA code, cookie, or API key; its OAuth grant is stored only in the operating-system credential vault.

The goal is not to manufacture a high score or imply guaranteed profit. The goal is to make each idea traceable from source evidence to exact instrument, expose when the evidence is stale or adverse, size risk before capital is committed, and stop the workflow when a required fact cannot be verified.

> [!IMPORTANT]
> Optedge is research and decision-support software. It does not guarantee returns, does not autonomously trade, and does not make an order safe merely because Robinhood accepts it. The current local connection exposes allowlisted one-shot reads and broker previews, but no placement API. Any later order decision remains outside Optedge's current release and requires explicit review in a Robinhood-supported surface.

[Quick start](#quick-start) · [Decision workflow](#decision-workflow) · [Model firewall](#model-promotion-firewall) · [Edge Lab](#edge-lab) · [Dashboard](#dashboard) · [Robinhood review](#manual-robinhood-review) · [Project map](#project-layout) · [Documentation](#documentation)

## Project Status

| Layer | Current role |
|---|---|
| Research scanner | Ranks multi-asset candidates and records the evidence used. |
| Trade Desk | Presents the decision, exact candidate identity, risk plan, freshness, and blockers. |
| Edge Lab | Fails closed unless independent, current-method, after-cost evidence passes every live-review requirement. |
| Model firewall | Keeps ordinary scans inference-only and rejects adaptive artifacts that lack exact, fresh, purged out-of-sample promotion evidence. |
| Capital firewall | Applies a chained same-account equity history, drawdown-scaled risk, and fail-closed portfolio checks before a review packet can exist. |
| Robinhood connection | Direct official MCP OAuth, allowlisted one-shot reads and reviews, and a manual packet fallback; no placement API or unattended loop. |
| Live-capital readiness | Determined by current local evidence and broker state at review time, never by this README or an account permission alone. |

## What Optedge Is

- A local research cockpit that runs on your machine.
- A multi-asset signal ranking system for options, shares, futures, and value ideas.
- A dashboard and validation layer for reviewing exact candidates, open research positions, closed outcomes, and model evidence.
- A lifecycle tracker that stores open and closed recommendations locally.
- A research tool with archive/reset support and safety guardrails.
- An inspectable Python codebase with no paid Optedge service, hosted dashboard, or required subscription.

## What Optedge Is Not

- Not financial advice.
- Not a profit guarantee.
- Not an autonomous execution engine.
- Not a replacement for human review.
- Not production-ready for live capital without strong validation evidence.
- Not a broker, exchange, market-data guarantee, tax service, or portfolio-management service.

## Quick Start

Python `3.11` through `3.13` is supported; Python `3.12` is recommended.

```powershell
git clone https://github.com/kaiuribess/Optedge.git
cd Optedge
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
$env:OPTEDGE_CONTACT = Read-Host "Real operator email for SEC requests"
python setup_check.py
```

Start with synthetic data if you want to inspect the workflow without relying on live providers:

```powershell
python run.py --demo --no-open
python run.py --cockpit
```

For a normal local scan and the decision-first cockpit:

```powershell
python run.py --no-open
python run.py --validation-report
python run.py --cockpit
```

The cockpit opens on `http://127.0.0.1:8765`. Generated research stays under the Git-ignored runtime paths in `data/`, `logs/`, and `telemetry/`. A new installation is expected to be paper-only until independent evidence matures.

## Decision Workflow

```text
Discover candidates
  -> validate data freshness and exact instrument identity
  -> compare the top three exact setups with No Trade
  -> inspect factor agreement and adverse evidence
  -> verify the active model and weight identities
  -> check Edge Lab's independent after-cost results
  -> calculate stop-based risk and proposed capital at risk
  -> apply the same-account high-water/session drawdown interlock
  -> prove same-account broker exposure and post-trade headroom
  -> request one broker preview
  -> stop inside Optedge; the current release has no placement API
  -> if you still choose to trade, re-review and confirm in a Robinhood-supported surface
  -> monitor any resulting broker order or position in Robinhood
```

Every boundary is intentional. A high research score cannot bypass Edge Lab, stale data cannot become fresh because a file was recently written, an option cannot silently fall back to a share order, and options approval cannot substitute for buying power, liquidity, evidence quality, or user confirmation.

## Core Features

- Options mispricing, chain analytics, surface checks, and contract ranking.
- Share and value ranking using the full non-option factor stack.
- Futures ranking using futures trend, macro, risk, volatility, and cross-asset context.
- Multi-factor fusion across sentiment, news, fundamentals, filings, macro, technicals, market structure, and retail attention.
- EV, slippage, fractional Kelly sizing, bankroll caps, and sector concentration controls.
- Multi-asset lifecycle tracking for open and closed recommendations.
- Dynamic exit pressure reviewed every scan.
- Conservative self-learning exit policy that stays inactive until enough evidence exists.
- Validation report, factor IC summary, equity curve, and position aging output.
- Edge Lab with exact policy provenance, evidence-lane separation, horizon-length block intervals, complete-cost reconciliation, benchmark excess, time stability, and option outcome-quality checks.
- A freshness-gated comparison board that shows at most three exact setups beside **No Trade / hold cash**, without comparing incompatible raw scores across assets.
- An adaptive-model promotion firewall: normal scans do not train, persist, or immediately consume a challenger, and unsafe artifacts fall back to a zero return predictor plus source-controlled weights.
- Interactive local HTML dashboard.
- Free local cockpit server with symbol lookup across latest scan artifacts.
- Exact candidate handoff for options: symbol, call/put side, strike, expiration, underlying type, quote provenance, and a deterministic candidate fingerprint stay attached to the plan.
- Separate option profiles: the normal swing lane keeps its `90+` DTE default, while explicit `leaps_swing` candidates use a profile-isolated `365-900` DTE policy and evidence lane.
- A direct official Robinhood MCP connection with browser OAuth, operating-system-vault token storage, a narrow read/review allowlist, and no exposed placement method.
- Safe archive/reset tool for generated research artifacts.
- Research guardrails for sample size, drawdown, spreads, stale models, and data health.
- A same-account total-open portfolio gate that separates real broker exposure from research and paper state.
- A durable, pseudonymous Robinhood equity ledger that scales or blocks new-entry risk after account losses, keeps the real-data default outside the checkout, and detects missing-primary or rolled-back history with an atomic backup sidecar.

## Signal Coverage

Optedge is intentionally broad. Each scan can combine many independent signals, but the goal is not to trust every factor blindly. The goal is to make the evidence visible, size ideas conservatively, and validate which signals are actually helping.

| Area | Coverage |
|---|---|
| Options pricing, surface, and flow | Black-Scholes, CRR binomial, Bjerksund-Stensland, CBOE theoretical price, ensemble weights, IV rank, IV premium, directional buyer/seller edge after spread, skew, surface anomalies, DTE, delta, open interest, bid/ask spread, unusual options activity, put/call ratios, and contract-level call/put ranking |
| Sentiment, social, and retail attention | WSB, r/options, StockTwits-style social signals, ApeWisdom/Twitter-style attention, FinBERT, VADER, keyword/degen-aware scoring, Google Trends, and attention momentum |
| News, earnings, and catalysts | Recent headlines, headline sentiment, news momentum, earnings calendar, days-to-earnings, whisper signals, IV-crush risk, FDA/biotech catalysts, and event proximity |
| Fundamentals and value | Market cap, valuation, quality, P/E, FCF yield, earnings yield, EV/EBITDA, SEC companyfacts balance-sheet context, margin/ROIC proxies, deep value buckets, Graham-style score, and Magic Formula-style quality/value composite |
| Insider, filings, and Congress | SEC Form 4 parsing, recent SEC filing lookup, insider buys/sells, officer/director weighting, Finnhub MSPR aggregate insider sentiment, Form 144 planned sales, buybacks, 13F context, cluster-buy detection, and STOCK Act disclosures |
| Macro, rates, credit, and volatility | VIX, SPY momentum, Treasury yields, curve slope, CPI, unemployment, Fed funds, HY/IG credit spreads, keyless FRED CSV fallback, and volatility regime context |
| Futures, commodities, and crypto | Equity index, rates, energy, metals, agriculture, crypto futures, trend/range/volatility features, CFTC CoT, EIA energy data, USDA WASDE, and Hyperliquid-style crypto context |
| Market structure and technicals | Dark-pool/FINRA short-volume proxy, SEC fails-to-deliver context, short interest, squeeze setups, sector ETF flows, trend, momentum, RSI, MACD, relative strength, 52-week range position, and volatility regime |
| Risk, portfolio, and telemetry | Sector concentration, portfolio Greeks, drawdown breaker, research guard report, engine health, empty-engine diagnostics, and engine latency telemetry |

## Multi-Asset Trade Lifecycle

Every scan can add new qualified recommendations, reprice existing open recommendations, review exits, and update local open/closed state files.

### Options

- Uses the full research stack plus option-chain data and option-specific pricing math.
- Prices, ranks, sizes, tracks, reprices, and closes recommendations.
- Applies hard exits for stop, target, and expiry.
- Runs dynamic exit review every scan after hard risk exits.
- Keeps the standard swing workflow at `90+` DTE by default.
- Offers an explicit `leaps_swing` profile for `365-900` DTE contracts. LEAPS refers to expiration runway, not a required holding period: the profile reviews the thesis after 3, 5, and 10 sessions and caps the planned hold at 20 sessions.

### Shares

- Uses the full non-option research stack for equity ideas.
- Tracks equity entries, current prices, suggested sizing, stops, targets, and dynamic exit pressure.
- Does not depend on option-chain fields.

### Futures

- Uses the full non-option research stack plus futures, macro, trend, volatility, and risk context.
- Uses ATR-like stop/target logic, point-value risk sizing, and micro futures preference when available.
- Reviews futures score reversals, volatility changes, macro context, and reprice failures every scan.

Shares and futures do not use option-specific fields such as strike, expiry, DTE, IV, delta, Black-Scholes, CRR, BJS, CBOE theoretical price, or option mispricing. All scanner outputs remain research recommendations. The local Optedge application does not submit orders.

## How a Run Works

1. Builds a universe from configured option/share lists, prior tracked names, and WSB trending discovery.
2. Filters the universe so slower engines focus on liquid, relevant, or attention-heavy names.
3. Runs live-data engines concurrently across options, news, filings, fundamentals, sentiment, macro, futures, technicals, and market-structure signals.
4. Prices option contracts with multiple models and logs model predictions for later scoring.
5. Scores social and headline text with local sentiment models when available.
6. Freezes forward-test metadata before outcomes are known, including the exact active predictor and runtime-weight identities, and keeps current executable, current shadow, and legacy research evidence separate.
7. Fuses factor scores into ranked calls, puts, share ideas, value plays, and futures setups.
8. Applies slippage, spread checks, fractional Kelly sizing, bankroll caps, sector caps, earnings-risk adjustments, and guardrails.
9. Adds qualified recommendations to local research position files.
10. Reprices open options, shares, and futures.
11. Applies hard exits, dynamic exit review, and conservative learned exit policy when evidence thresholds are met.
12. Builds the Edge Lab view from independent fixed-session outcomes and blocks live review when its requirements are not met.
13. Writes the dashboard, watchlist, signal logs, lifecycle state, telemetry, and validation outputs.

Source-controlled factor weights remain the default. The historical IC command compares today's factor snapshot with returns that have already happened, so it is explicitly labeled a look-ahead diagnostic and cannot promote weights or authorize live review. Any adaptive path must satisfy its own chronological, after-cost, sample-size, freshness, and concentration rules.

### Model promotion firewall

Ordinary scans are inference-only: they never fit, persist, or immediately consume new predictor coefficients or fusion weights. Research fits remain `shadow_untrusted`. A missing, legacy, stale, malformed, mixed-asset, or digest-invalid artifact falls back to the source-controlled safe state—a zero stock-return predictor and the committed factor weights—rather than being repaired or trusted silently.

An active stock-return champion must be share-only and carry fixed-horizon, after-cost, purged expanding-window out-of-sample evidence. A global runtime-weight champion must separately prove share and direct broker-observed option improvement. Option adaptation stays off until an option-specific path has direct broker-observed targets and passes that standard. Every forward signal freezes the model-trust state, active predictor SHA-256 identity, active runtime-weight SHA-256 identity, option-adaptation state, policy digest, and experiment ID; a later model change cannot inherit the earlier model's evidence.

## Edge Lab

Edge Lab is the cockpit's conservative evidence gate. It reads `data/fixed_horizon_outcomes.parquet`, scores independent 5-, 10-, and 20-session slices, and uses the configured 10-session horizon for the headline decision unless the validation summary specifies another supported headline.

Evidence is selected in this order, without mixing lanes:

1. `current_method_executable` — current strategy rows that were independently sampled and eligible for executable metrics.
2. `current_method_shadow` — current strategy rows frozen before portfolio guardrails but not eligible as executed evidence.
3. `legacy_research_only` — older or incompatible research retained for transparency.

Only the first lane can clear the live-capital evidence gate. A large legacy sample cannot authorize current-method review.

For an asset lane to show **Validated for manual review**, every requirement must pass:

| Requirement | Threshold |
|---|---:|
| Fresh policy-bound outcome and summary sources | No more than `96` hours old |
| Current-method executable outcomes | Required |
| Exact strategy, experiment, methodology, evidence-policy, and active-model provenance | Required |
| Resolution, return, cost, and SPY benchmark coverage | `100%` reconciled |
| Independent outcomes | At least `200` |
| Distinct entry days | At least `30` |
| Effective horizon-length blocks | At least `30` |
| Average return after recorded costs | Greater than `0` |
| 90% circular moving-block lower bound | Greater than `0` |
| Profit factor after costs | At least `1.15` |
| Average excess return versus SPY | Greater than `0` |
| Average return with costs doubled | Greater than `0` |
| First-half and recent-half daily average | Both greater than `0` |
| Option entry-spread coverage and recorded cost coverage | `100%`; cost must cover the entry spread |
| Broker-observed option outcome coverage | At least `50%` for options |

Rows opened on the same entry day are averaged before resampling, so a crowded signal day cannot masquerade as many independent trials. The 90% interval uses a deterministic circular moving-block bootstrap whose block length is at least the holding horizon. The live gate also requires 30 effective blocks; at the 10-session headline horizon that normally means roughly 300 distinct entry days, even though the separate minimum remains 200 outcomes across 30 days. Edge Lab reports results at `1.5x` and `2x` the recorded slippage assumption, early-versus-recent stability, and the split between broker-market observations and modeled option proxies. An option outcome records the greater of the configured cost floor and its entry spread; a missing entry spread is labeled rather than guessed and cannot satisfy the option coverage gate. For options, only exact broker-market-observed outcomes enter live performance metrics; modeled proxies remain research-only and can never improve the live verdict.

The explicit `leaps_swing` profile is more isolated than the general option row. It can use only outcomes stamped with both `execution_profile=leaps_swing` and `strategy_evidence_lane=option_leaps_swing`; ordinary option evidence cannot authorize it. Every 5-, 10-, and 20-session slice must independently pass with 100% broker-market-observed outcomes, no pending or excluded rows, and the same minimum sample, confidence, cost-stress, benchmark, and stability standards. This separates long-expiration contract selection from the much shorter 3/5/10-session thesis-review cadence.

`Validated` means the stored evidence cleared this gate; it is not a profit promise or an instruction to trade. `Promising` and `Paper only` remain research states. `Adverse`, `Fragile`, `Insufficient`, missing, or unreadable evidence blocks live review. See [Edge Lab methodology](docs/EDGE_LAB.md).

## Validation

Generate the validation report with:

```bash
python run.py --validation-report
```

Outputs:

- `data/validation_report.html`
- `data/validation_summary.json`
- `data/equity_curve.png`
- `data/factor_ic_summary.json`
- `data/position_aging_summary.json`
- `data/fixed_horizon_outcomes.parquet`
- `data/fixed_horizon_summary.json`
- `data/robinhood_option_history_requests.json`
- `data/robinhood_option_history_coverage.json`

Validation keeps the compatibility label `current_model`, but the default scope means the current unarchived experiment. The latest `archive.py` reset establishes the experiment boundary; ordinary model-weight updates do not hide outcomes. Open research positions are counted from the current lifecycle state files, while closed-position metrics only become meaningful after enough recommendations close.

Early reports are expected to show small-sample warnings. Fixed-horizon evidence scores one independent thesis per asset, ticker, direction, and entry day after 1, 3, 5, 10, and 20 completed sessions. Each signal and outcome carries the exact strategy, experiment, methodology, evidence-policy digest, model-trust state, active predictor identity, active runtime-weight identity, option-adaptation state, and execution profile that produced it; old, unstamped, profile-mismatched, or identity-mismatched rows remain visible as legacy research but cannot authorize current capital. A shadow row records that the current strategy passed before portfolio-level guardrails; this lets validation accumulate while actual sizing remains blocked. Shares and futures use observed historical closes. Options prefer exact, non-interpolated Robinhood option trade bars supplied by the direct read client or an externally collected connector cache, then fall back to a clearly labeled constant-entry-IV model proxy when no exact target-date bar exists. These are outcome marks, not verified Optedge fills. Learned exits remain inactive until minimum evidence thresholds are met. Negative, unstable, sparse, modeled, stale, incomplete, unreconciled, or uncorrelated results must be treated as blockers rather than explained away.

Build or inspect the bounded read-only option-history queue with:

```bash
python scripts/refresh_robinhood_option_history.py --status
```

The direct client may satisfy exact contract requests with allowlisted one-shot historical-bar reads after browser OAuth; its grant is retrieved from the operating-system credential vault and is never written to a project file. A connected Codex/Robinhood session can remain a manual read-only fallback. The next validation refresh upgrades matching proxy outcomes to broker-observed bars while preserving their provenance.

Use all-time validation only when you intentionally want older history included:

```bash
python run.py --validation-all-time
```

See [docs/VALIDATION.md](docs/VALIDATION.md) for details.

## Install

Windows:

```powershell
install.bat
```

Linux or macOS:

```bash
bash install.sh
```

Manual setup in PowerShell:

```powershell
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
$env:OPTEDGE_CONTACT = Read-Host "Real operator email for SEC requests"
python setup_check.py
```

The requirements file installs this checkout in editable mode. Runtime and
development dependency declarations live in `pyproject.toml` so installers,
CI, and the `optedge` command share one source of truth.

Optedge is intentionally source-first: use the installer or editable setup
above so private runtime state stays in this checkout's ignored `data/`
directory. A system-wide, non-editable install is not a supported trading-data
layout.

Python `3.11` through `3.13` is supported; Python `3.12` is recommended.

### SEC operator contact

SEC automated-data requests require an honest operator contact. Before running SEC-backed engines or the full setup check, set `OPTEDGE_CONTACT` to a real email address you control. Optedge does not invent a contact and rejects placeholder, example, `.local`, `.test`, and other non-contact addresses.

PowerShell can collect the value without placing the address directly in the command history:

```powershell
$env:OPTEDGE_CONTACT = Read-Host "Real operator email for SEC requests"
```

Linux and macOS:

```bash
read -r -p "Real operator email for SEC requests: " OPTEDGE_CONTACT
export OPTEDGE_CONTACT
```

For compatibility, a real email embedded in the older `SEC_USER_AGENT` value is still accepted, but `OPTEDGE_CONTACT` is the canonical setting. If neither setting contains a real address, SEC-backed requests fail before contacting the SEC. The address is included only in the SEC `User-Agent`; ordinary non-SEC requests use the version-only product identity `Optedge/<version>`.

Optional live/broker option-chain source:

```powershell
$env:OPTEDGE_TRADIER_TOKEN="your-production-tradier-token"
```

If no Tradier token is set, Optedge stays on the free chain stack: CBOE delayed quotes, NASDAQ chains, bounded Yahoo options JSON, then yfinance fallback.

Optional local FinBERT sentiment scoring:

```bash
python -m pip install -e ".[sentiment]"
python diagnose_gpu.py
```

The `sentiment` extra installs the standard PyTorch and Transformers packages.
CPU inference works without an NVIDIA GPU. For GPU acceleration, use the
official PyTorch install selector to install the wheel that matches the current
operating system and driver, then rerun `python diagnose_gpu.py`. FinBERT output
is a fallible research classification; it is not performance evidence.

## Run

Single scan:

```bash
python run.py
```

After installation, the equivalent console entry point is also available:

```bash
optedge --help
```

Aggressive research mode with a custom bankroll:

```bash
python run.py --aggressive --bankroll 25000
```

Loop every 30 minutes:

```bash
python run.py --aggressive --bankroll 25000 --loop 30
```

Loop every 30 minutes without opening a browser:

```bash
python run.py --aggressive --bankroll 25000 --loop 30 --no-open
```

Loop mode sleeps after each run completes. It is not exact wall-clock scheduling.

Faster loop when SEC insider parsing is slow:

```bash
python run.py --aggressive --bankroll 25000 --loop 30 --fast-insider
```

Turbo loop using RAM cache, batched GPU FinBERT when CUDA is available, and faster insider parsing:

```bash
python run.py --aggressive --bankroll 25000 --loop 30 --turbo --no-open
```

`--turbo` does not place trades. It keeps the normal engine stack, enables the in-process RAM cache, raises FinBERT batch size, and switches insider parsing to the faster count-only mode.

All `--loop` examples above are local research refreshes. They do not schedule a Codex task, send recurring Codex messages, initiate a Robinhood preview, or take an external order action.

Forward test logged signals:

```bash
python run.py --forward
```

The command shows mixed-age current marks as monitoring telemetry and separately writes leakage-resistant fixed-session outcomes. Only current-method, independently sampled, executable rows can enter the fixed-horizon headline.

Historical factor IC backtest:

```bash
python run.py --backtest
```

This command is a **look-ahead diagnostic**, not a tradable backtest: it compares current factor scores with already-realized returns. Its output is labeled `diagnostic_only_lookahead`, is ineligible for model promotion, and must not be cited as proof of edge. Use fixed-session forward outcomes and Edge Lab for decision evidence.

Heston pricing stability check:

```bash
python run.py --heston-stability
```

Instant local lookup from the latest scan artifacts:

```bash
python run.py --lookup NVDA
```

When the authenticated Robinhood MCP connector is available, lookup also writes a bounded read-only refresh request. A user-triggered direct read, or a separately connected Codex/Robinhood session used as a manual fallback, can supply Robinhood's current equity quote, official close, fundamentals, earnings timing, recent price history, and the exact option contract's mark, spread, Greeks, volume, open interest, tradability, and history. Broker quote timestamps drive freshness labels. Large differences between a saved local option mid and the broker mark block the swing verdict until the local chain is refreshed. No account number, credential, position, order, or fill is stored in this research cache.

Inspect the read-only lookup cache and queue with:

```bash
python scripts/robinhood_research_bridge.py --status
```

## Dashboard

Each scan writes a local dashboard to `data/dashboard_*.html` and opens it in the browser by default unless `--no-open` is used.

The dashboard includes:

- Macro regime and run statistics.
- Edge Lab status by asset, with evidence lane, sample independence, after-cost performance, confidence bounds, and the first unmet requirement.
- Current research analytics and research-lifecycle open-position P&L charts.
- Factor IC and open-position aging charts.
- Calls, puts, shares, value, and futures cards.
- Search, sort, ready/watch filters, asset filters, compact mode, and expandable sections.
- Engine telemetry, rolling engine health, and empty-engine diagnostics.
- TradingView watchlist export.

Generated dashboards are ignored by Git so local research output stays private.

## Local Cockpit

Run a small local browser cockpit without paid services or extra dashboard hosting:

```bash
python run.py --cockpit
```

The cockpit opens at `http://127.0.0.1:8765` by default and reads local files from `data/`. It refuses non-loopback/LAN bindings, rejects unknown Host headers, and protects every state-changing request with a per-launch same-origin token. It gives you:

- A decision-first **Trade Desk** as the default screen: market regime, evidence quality, validation/risk gate, and Robinhood review readiness.
- **Evidence Mission Control** for current-method executable/shadow samples, the isolated LEAPS lane, exact Robinhood option-history coverage, pending contracts, and explicit legacy-strategy quarantines. Collection remains user-triggered; the dashboard never starts a polling or trading loop.
- A freshness-gated **top three versus No Trade** comparison. It ranks lexicographically by source provenance, Edge Lab eligibility, blockers, execution quality, slippage-adjusted reward/risk, then catalyst/regime support; raw scores from different asset models are never treated as comparable.
- An **Edge Lab** stage that makes adverse, fragile, insufficient, and validated evidence visually distinct and shows the data timestamp behind the decision.
- A stop-based trade planner for whole shares and long calls/puts, with risk budget, proposed capital at risk, a total-open same-account allocation cap, slippage, planned stop loss, reward/risk, and breakeven win rate.
- Side-by-side model and capital firewalls showing whether source-controlled defaults or a trusted champion are active, whether ordinary-scan training is off, and whether current same-account drawdown evidence allows full, reduced, or zero new-entry risk.
- One short-lived manual Robinhood review packet you can copy or save as an inspection copy, plus a direct two-click option path. The direct path first requests Robinhood's complete preview, then issues a 60-second single-use confirmation for that exact order. It explicitly forbids loops, scheduled tasks, repeated orders, and automatic retries.
- Exact candidate preservation from research card to planner. Options and shares both need a fresh, actionable source candidate before a broker packet can be copied; an incomplete option identity is blocked instead of being converted into a share plan.
- Instant symbol lookup across latest option, share, value, futures, and open-position artifacts.
- Read-only Robinhood ticker and exact-option context when the connector cache has a matching record, with explicit quote age and source labels.
- Focused scan launcher: type a ticker, company name, or option idea and click **Run focused scan**.
- Full/quick focused-scan modes, optional bankroll override, and aggressive sizing toggle.
- Active and expired option/share/futures research-lifecycle counts, labeled separately from verified broker holdings.
- Active and expired research positions separated so expired rows cannot inflate current exposure.
- Quick links to the latest dashboard, validation report, validation JSON, option-history and broker-research queues, equity curve, and external paper-order export.
- A browser UI that does not rerun engines until you choose to run a new scan.
- A local-only first screen that never waits on free live market providers. It uses a fresh saved Swing Climate snapshot when one exists; otherwise it shows a conservative `context_unavailable` posture and keeps defensive gates active until you explicitly refresh the deeper research view.
- A direct connection panel for Robinhood's official Trading MCP. OAuth opens in the browser, grants are kept only in the operating-system credential vault, and the cockpit exposes explicit connect, status-refresh, one-shot complete snapshot-sync, shortlist-check, order-preview, confirmed option-placement, and disconnect actions. The client has no generic broker-tool dispatcher and the fixed placement boundary is reachable only through a consumed single-use confirmation.
- A **Check top 10 on Robinhood** action that preserves normal Optedge ranking and inspects up to the first ten exact queue candidates through bounded Robinhood chain/instrument/quote reads. Each row shows contract uniqueness, quote age, spread, frozen price cap, liquidity, Greeks, and every blocker. A passed market result expires after 120 seconds and cannot override normal evidence, validation, account, drawdown, or exposure gates.
- A **Capture checked evidence** action that freezes one still-fresh, source-bound live finalist as an immutable paper signal. It makes no additional broker call, is idempotent for the same finalist digest, stamps the complete current evidence/model policy, and keeps portfolio-blocked rows in the shadow lane rather than pretending they were executable.
- A **Sync 5 exact histories** action that resolves a small active-contract batch through allowlisted Robinhood reads, requires exact chain/instrument identity, reads regular-session daily bars, and atomically commits the batch only after every selected contract succeeds. It has no retry loop, review call, or placement path.

### Manual Robinhood Review

Optedge connects to Robinhood's official Trading MCP endpoint through browser OAuth. The dashboard never asks for a Robinhood password, MFA code, cookie, or API key. OAuth tokens and dynamic client-registration material are stored only in the operating-system credential vault, with no plaintext file or environment-variable fallback. On Windows, an OAuth envelope that exceeds Credential Manager's single-entry limit is split into integrity-checked, generation-bound vault entries and committed through a small vault-resident manifest; no chunk is written to the repository or another plaintext location. The public client surface is intentionally limited to allowlisted one-shot reads and order reviews plus one fixed confirmed-option placement call; there is no generic tool dispatcher or generic placement method.

This is a direct connection: Codex is not required to hold the Robinhood connection open. Codex can still be used as a manual fallback by copying one short-lived review packet into a separately connected Robinhood task. Neither path creates a schedule, polling trade loop, batch, or automatic retry.

#### Options permissions

A long call or put requires one same active, funded, Agentic-accessible Robinhood account with options level 2 or 3. If options level 2 was newly enabled, connect and run **Sync broker snapshot once** before expecting the readiness panel to change. The permission only establishes account eligibility for supported long options; it does **not** validate the strategy, guarantee an order review, bypass model, evidence, drawdown, buying-power, overlap, or spread checks, enable multi-leg/short strategies, or authorize submission.

1. Set up an eligible account using [Robinhood's Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/).
2. Start the cockpit, choose **Connect Robinhood**, and complete Robinhood's browser OAuth screen. If the operating-system credential vault is unavailable, Optedge fails closed instead of saving the grant elsewhere.
3. Choose **Sync broker snapshot once**. It reads every returned account's portfolio, positions, and orders exactly once, follows only bounded proven cursors, joins held option IDs to instrument metadata, and fails before replacement if any account scope is incomplete. Raw account identifiers stay in memory; only the recursively redacted `optedge_robinhood_broker_snapshot_v1` and pseudonymous equity ledger are persisted. The manual fallback remains `python scripts/normalize_robinhood_broker_snapshot.py` with the account-scoped `optedge_robinhood_mcp_read_bundle_v2` format documented in [Third-Party Forward Testing](docs/THIRD_PARTY_FORWARD_TESTING.md). In either path, the broker source time must be no more than 45 minutes old and local normalization cannot renew stale data.
4. For an option, choose the explicit evidence profile in the Review Desk and build a fresh queue. The default `swing_execution` lane remains `90+` DTE. To opt into LEAPS, choose **True LEAPS swing evidence** or run `python scripts/export_robinhood_agentic_queue.py --account-budget 500 --execution-profile leaps_swing --refresh-chain --chain-preset leaps`; that separate lane requires `365-900` DTE, explicit `option_leaps_swing` evidence, 3/5/10-session thesis reviews, and a 20-session maximum planned hold. A long expiry does not require a long hold, and DTE alone never changes the profile. In either lane, the queue is not a packet or broker authority, the exact contract must occur once and identically in the fresh cycle and queue, and every no-execution control must remain intact. See [LEAPS Swing Profile](docs/LEAPS_SWING.md).
5. In the Robinhood connection panel, choose **Check top 10 on Robinhood**. Optedge keeps queue order unchanged and checks at most `orders[0:10]`; it does not hunt for replacement contracts after seeing live prices. Every row must be identical in the fresh cycle and queue, resolve through bounded pages to exactly one active, tradable, standard 100-share contract, and pass a quote no older than 120 seconds, the frozen price cap, profile spread ceiling, liquidity minimums, and LEAPS delta rules when applicable. A passed Robinhood quote is market evidence only and cannot clear a blocked local Optedge entry gate. The saved batch contains sanitized public contract/quote evidence, not credentials, raw account identity, an order, or broker authority.
6. Choose **Sync 5 exact histories** to work down the active-contract history queue in a bounded all-or-nothing batch. Then run validation to upgrade matching modeled option outcomes to exact broker-observed market bars. A history bar is still not proof of an Optedge fill.
7. In **Trade Desk**, load the checked option candidate or an exact fresh, actionable `top_shares_*.parquet` candidate, verify the account-equity/risk/allocation assumptions and proposed entry, stop, and target, then click **Calculate plan**. Free-form share inputs can still calculate local sizing, but they cannot produce a copyable Robinhood packet. An ordinary share scan preserves the last history-bar close and its market session, then derives deterministic entry, stop, and target geometry from that reference. The reference is not a live quote: its attestation may explicitly say `candidate_quote_available=false`, and the direct preview client or manual fallback task must still obtain a fresh bid/ask before review. The manual gate binds the share symbol, direction, geometry, size cap, price-reference provenance, source digest, row fingerprint, actionability, guard status, and artifact freshness so an arbitrary planner symbol cannot borrow another share's evidence. It also requires current Edge Lab and validation evidence plus one same active account that satisfies the chained drawdown interlock, portfolio value, permissions, conservative buying power, per-trade risk, and the total-open allocation check. It recomputes `current broker capital at risk + proposed capital at risk` against `min(planner equity, same-account live equity) x allocation fraction`; it never mixes capacity, loss history, or exposure across accounts.
8. If one row clears every gate, choose the eligible masked Agentic account and click **Review exact order**. Optedge refreshes the exact contract, quote, portfolio capacity, positions, and working orders, then calls Robinhood's review tool. The direct placement path currently requires the selected Agentic account to have no open positions or working orders; use the inspection/manual review path when managing an already invested account. It does not place the order during this step.
9. The direct client or fallback task must independently refresh the exact derived account identity, portfolio, positions, working orders, instrument, tradability, and live quote immediately before review. Every collection must reach an explicit final page. A failed, missing, stale, or ambiguous read blocks review. For an option, all matching chains and instruments must resolve to exactly one active buy-to-open standard contract with the planned underlying. The review uses a quote no older than 120 seconds and a spread no wider than the packet cap: at most `1%` for shares, `15%` for normal swing options, and `10%` for `leaps_swing`. The limit cannot rise. Only then may the review tool show the complete preview, disclosures, alerts, fees, collateral, and estimated cost.
10. Stop at the preview unless you deliberately want the real-money order. To submit, check the explicit acknowledgement and press **Place this exact order once**, then accept the final browser confirmation. The 60-second in-memory capability is consumed before placement; Optedge rechecks the selected Agentic account, capacity, positions, working orders, exact candidate, contract, and ask, calls only `place_option_order` once, and never retries an ambiguous or failed result. A submission is still not a fill, so verify order status in Robinhood.

The broker boundary is manual and on demand. Optedge does not create a recurring Codex task or automatic trade loop. The Robinhood queue and local auto-paper script are research/paper artifacts; neither is broker authorization. A direct placement requires an immediately preceding clean broker preview and its unexpired single-use in-memory capability. Packet v2 remains inspection-only and includes canonical semantic and prompt digests; those digests detect modification but are not signatures, authentication, or standalone authority.

Each direct live order receives a fresh UUID `ref_id` only after the broker preview clears. The same identifier is used for that one logical placement attempt and is never recycled into another order. It is idempotency context, not placement authority. Optedge never automatically retries.

#### Capital-loss firewall

The general deterministic planner rejects risk above `2%` of account equity and a total-open allocation fraction above `25%`. The manual Robinhood review boundary is stricter: it starts from a `1%` per-trade risk ceiling and can only reduce it. The exact plan must fit the resulting same-account ceiling:

| Account state from the chained equity ledger | Maximum manual-review risk |
|---|---:|
| Less than `5%` below observed high water | `1.00%` of account equity |
| At least `5%` but less than `8%` below high water | `0.50%` |
| At least `8%` but less than `10%` below high water | `0.25%` |
| At least `10%` below high water | New entries blocked |
| At least `3%` loss for the current New York session | New entries blocked |

The v2 interlock requires at least two strictly ordered, hash-chained observations spanning at least `18` hours and at least two New York calendar dates; a latest observation no more than 90 minutes old; an exact match to the current normalized snapshot; and the same pseudonymous account throughout. The separate broker-readiness gate is stricter at 45 minutes, so a valid ledger does not make an aging snapshot reviewable. The real repository `data/` directory stores this ledger outside the checkout: `OPTEDGE_STATE_DIR` can select the directory explicitly, otherwise Optedge uses the per-user OS state directory (`%LOCALAPPDATA%\Optedge\risk` on Windows or `$XDG_STATE_HOME/optedge/risk`, with the normal home-directory fallback, on Unix-like systems). Explicit custom and test data directories remain self-contained under their own `robinhood_account_equity_ledgers/` folder.

Each file replacement is atomic, and a successful append leaves `account_<digest>.json` and `account_<digest>.json.bak` sealed to the same newest chain. A missing primary or required sidecar, divergent/rolled-back history, or a sidecar that lags the primary blocks review. A validated lagging sidecar left by an interrupted final replacement can be resealed only by an explicit normalization; that recovery copies the already-validated newest chain and does not invent an observation or create a new baseline. Optedge never automatically rebaselines. Stale or malformed history, snapshot mismatch, or a mixed account also blocks new entries. An unexplained adjacent equity change of at least `25%` likewise needs a deliberate operator rebaseline because deposits and withdrawals cannot safely be mistaken for trading P&L. Exact snapshot equality is intentionally strict; when normal market activity keeps changing broker fields, capture, normalize, and review in a stable state—often after hours—instead of weakening the comparison. The packet binds the ledger and snapshot digests plus the arithmetic behind the drawdown-adjusted risk ceiling; none of these controls guarantees a limited loss or a profitable trade.

A new account begins in **warming up**, not “unsafe.” The first explicit snapshot creates observation one. Review remains blocked until a later explicit snapshot creates at least observation two, the observations span at least 18 hours, and they cover at least two New York calendar dates. Optedge displays those exact requirements and never backdates, duplicates, or fabricates an observation to clear the gate. A malformed, rolled-back, or inconsistent ledger is still labeled unsafe and remains a hard block.

Current packet support is intentionally narrow: exact-candidate long share/ETF buys and conservatively verified standard single-leg long calls or puts on equity/ETF underlyings, using limit good-for-day orders during regular hours. An option needs a `100x` multiplier, active buy-to-open tradability, and an exact nonnumeric chain root matching its underlying. Multiplier `100` alone is insufficient: any live metadata or preview that identifies, suggests, or cannot resolve a nonstandard deliverable blocks the order. This detection is deliberately conservative because adjusted-contract metadata is not equally complete across every surface. Index options (including `^` symbols and known roots such as SPX, NDX, RUT, and VIX), missing/non-equity `underlying_type`, short shares, short options, spreads, adjusted or unresolved option deliverables, market orders, futures, and crypto are blocked. Every nonterminal broker option order must contain exactly one valid object leg to establish exact identity; multi-leg or malformed-leg orders block review instead of having extra legs discarded. Existing option positions or working open orders in the same symbol and long-call/long-put direction block a new entry even when strike or expiry differs. A same-symbol holding across asset types is also blocked: existing shares prevent a new option entry on that underlying, and existing options prevent a new share entry, until the cross-asset concentration is reviewed outside this entry flow. An equity review needs an active, funded, agentic-accessible account with explicit portfolio value. An option review needs one and the same account to be active, agentic-accessible, funded, and approved for options level 2 or 3; permissions or capacity split across accounts do not qualify. Options level 2 supports the narrow long-call/long-put permission checked here, but it is only permission—not evidence, suitability, liquidity, affordability, or authorization. Live spreads are capped at `1%` for shares, `15%` for normal swing options, and `10%` for `leaps_swing`, with any stricter candidate cap preserved.

V2 broker readiness uses only a positive `portfolio.total_value` plus both explicit `buying_power` and `unleveraged_buying_power`; it does not substitute equity aliases or cash. Reconciliation assigns each account the versioned `acct_` key derived above and compares broker holdings only with local lifecycle rows explicitly marked as broker-linked. The `...last4` mask is a display aid and can be shared by multiple accounts; it can never select or join an account. Ordinary Optedge research recommendations and Agentic paper rows remain visible as separate informational counts and do not create a live holding or reconciliation mismatch. Exact account key, direction, aggregate quantity, and option-contract identity must agree, so same-nickname accounts, long/short differences, and quantity differences cannot be mistaken for a match.

The total-open portfolio gate derives current exposure only from one immutable in-memory capture of fresh normalized same-account broker positions and orders. It does not count research recommendations or paper positions as real capital. Long shares reconcile absolute market value with quantity times a valid current price and block materially conflicting values; long options contribute quantity times `100` times the most conservative valid ask/mark/current value. Invalid or contradictory quantity aliases, duplicate/blank account identity, short, ambiguous, adjusted, expired-but-nonzero, unmarked, unscoped, pending-transition, or same-account working-order states block review instead of being converted to zero or guessed.

The entry packet does not place a stop or target order. Those values are planning references only. For a long option, the maximum capital-loss reference is the full debit; for a long share, the capital-at-risk basis is full entry notional. The proposal must fit the per-trade risk rules, conservative buying power, and remaining total-open same-account headroom. A stop is not guaranteed to limit a gap loss.

Optedge's current packet is an **entry review**, not a complete position-management system. Order cancellation, exercise, assignment, expiry handling, sell-to-close decisions, and emergency exits must be verified and managed through Robinhood's supported surfaces. Never assume the planning stop exists at the broker.

Use another port or keep it from opening a browser:

```bash
python run.py --cockpit --port 8777 --no-open
```

For a ticker that is not in the latest artifacts, run a focused scan first:

```bash
python run.py --universe NVDA --no-open
```

The cockpit run button does this for you in the background. Company-name resolution uses a free Yahoo search endpoint where available, so `Nvidia` can resolve to `NVDA`; direct tickers always work. Option-style requests such as `AAPL 20260618 C 200` are stored with the focused scan job so the cockpit remembers the contract you wanted checked while the scanner researches the underlying. Completed jobs link to their own generated dashboard and expose a log tail for review.

## Archive / Reset

`archive.py` is a safe reset button for generated run data. It moves files into `archive/run_YYYYMMDD_HHMMSS/`, preserves subfolder structure, and does not delete source code.

Archive generated run data:

```bash
python archive.py
```

Preview first:

```bash
python archive.py --dry-run
```

Archive/reset while keeping learned adaptive files:

```bash
python archive.py --keep-learned
```

Default archive mode moves learned/adaptive files too, which is useful for a fully clean experiment reset. `--keep-learned` preserves:

- `data/model_weights.json`
- `data/exit_policy.json`
- `data/exit_policy_history.jsonl`
- `data/exit_reviews.jsonl`

Archive/reset does not move source code, docs, tests, config, requirements, or GitHub workflow files.

## Validation Report

The validation report is the main proof layer for the research loop. It reports:

- Total signals.
- Closed versus open positions by asset.
- Win rate, average return, median return, profit factor, and max drawdown.
- Calls versus puts performance.
- DTE, spread, and confidence bucket performance.
- Factor IC and open-position age buckets.
- Dynamic exit actions and learned exit policy status.
- Performance after estimated slippage.
- SPY and QQQ benchmark comparison when market data is reachable.
- Random baseline comparison.
- Sample-size warnings.
- Independent 1/3/5/10/20-session outcomes with 95% win-rate intervals and SPY/QQQ excess returns.
- Explicit outcome quality labels that keep observed stock/futures closes and exact Robinhood option bars separate from modeled option proxies.
- Edge Lab eligibility inputs, including independent entry-day counts, cost stress, benchmark excess, time stability, and option observation coverage.

See [docs/VALIDATION.md](docs/VALIDATION.md) for details.

## Research Guardrails

Optedge includes a research safety layer in `risk/research_guard.py`.

It warns or blocks trust when:

- The closed-signal sample is under 500.
- Max drawdown is worse than `-20%`.
- Spread bucket performance is negative.
- An option recommendation has a spread above `15%`.
- Win rate is below a simple breakeven threshold.
- Model updates appear stale.
- Key data sources return no data.
- Key engines have weak rolling health.

Research Guard is supposed to be conservative. A warning is not cosmetic; it means the output needs more evidence or more human skepticism. If validation is weak, negative, sparse, or uncorrelated, Optedge should not be treated as reliable.

Research drawdown and broker-account drawdown are deliberately separate. Research drawdown controls use the validated strategy equity curve to reduce or block trust in the method; the Robinhood account-loss firewall uses only that account's current hash-chained broker-equity observations to reduce or block a new manual-review packet. Neither one can substitute for the other.

## Data Sources

Optedge uses free or locally configured sources where possible. A source appearing here means the code has an integration path; it does not mean that source is always reachable, real-time, complete, licensed for redistribution, or active in a particular run.

- Options chains and price history through a layered provider path, with optional official Robinhood MCP reads or externally collected connector caches for exact contract bars and interactive exact-contract quote checks.
- Cboe daily market statistics and total/equity/index put-call ratio CSVs for delayed options sentiment context.
- FRED public graph CSV macro context for configured credit, rates, labor, inflation, growth, and liquidity series.
- Nasdaq Trader symbol directory for broader official ticker/ETF search and universe hygiene.
- Nasdaq public stock screener for delayed small-cap mover discovery in Swing Scout.
- Reddit and retail-attention feeds.
- SEC EDGAR filings.
- SEC fails-to-deliver files for delayed settlement-pressure context.
- News and earnings feeds.
- Macro, rates, credit, energy, agriculture, volatility, and futures context.
- Optional FinBERT sentiment scoring when the local environment supports it.
- Optional Tradier production token for broker/live option chains; free CBOE/NASDAQ/Yahoo/yfinance fallbacks remain the default.

Some sources are delayed proxies; some may rate-limit, change schema, return partial data, or require a local key. SEC-backed paths additionally require the real operator email described in [SEC operator contact](#sec-operator-contact). Source failures should produce neutral, unavailable, or blocked states rather than fabricated evidence. Private keys are ignored by Git.

See [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

## Project Layout

> [!NOTE]
> GitHub's text beside each file or folder is the latest commit message that touched that path, not a customizable description. A professional commit should describe its complete change set; rewriting one commit per file merely to alter that column would damage useful history. This map, the directory READMEs, docstrings, and purpose comments are the canonical descriptions.

The [complete project map](docs/PROJECT_MAP.md) gives a maintained one-line
purpose description for every repository file. The shorter table below is the
operator-focused overview.

| Path | Purpose |
|---|---|
| `.github/` | Continuous-integration and repository workflow configuration. |
| `CONTRIBUTING.md` | Development workflow, evidence requirements, and pull-request expectations. |
| `SECURITY.md` | Private vulnerability-reporting and sensitive-data guidance. |
| `CODE_OF_CONDUCT.md` | Community participation and enforcement expectations. |
| `run.py` | Minimal source-checkout launcher; delegates all routing to `optedge.cli`. |
| `run.bat` | Windows convenience launcher for the source checkout. |
| `optedge/cli.py` | Routes commands to scans, symbol lookup, loop mode, or the local Trade Desk. |
| `optedge/orchestrator.py` | Coordinates the research engines, ranking, risk controls, tracking, and output generation. |
| `config.py` | Versioned research defaults, universes, limits, feature flags, and risk settings. |
| `async_http.py` | Shared bounded asynchronous HTTP client and provider-request helpers. |
| `setup_check.py` | Verifies Python, network data sources, and optional provider readiness before a live run. |
| `data_provider.py` | Historical-price and quote-provider access with bounded fallbacks and status reporting. |
| `chain_provider.py` | Option-chain acquisition and source fallback logic. |
| `pricing_models.py` | Option-pricing models and shared contract valuation helpers. |
| `universe_filter.py` | Narrows a broad ticker universe before expensive engines run. |
| `demo_data.py` | Generates synthetic first-look inputs; never represents live evidence. |
| `utils.py` | Shared numerical, retry, parsing, time, and serialization helpers. |
| `archive.py` | Safely moves generated experiments into timestamped archives. |
| `engines/` | Independent factor and data engines: technicals, fundamentals, sentiment, filings, macro, flow, and catalysts. |
| `fusion/` | Combines normalized engine evidence into ranked option, share, value, and futures ideas. |
| `backtest/` | Position sizing, lifecycle tracking, exits, calibration, forward testing, and model evaluation. |
| `backtest/edge_lab.py` | Builds the conservative current-evidence matrix and live-review eligibility verdict. |
| `backtest/leaps_edge.py` | Builds the profile-isolated LEAPS 5/10/20-session evidence gate. |
| `risk/` | Research guardrails, account drawdown interlocks, same-account portfolio exposure controls, and review-only trade-plan packet construction. |
| `optedge/leaps_swing.py` | Scores explicit `365-900` DTE LEAPS swing candidates against liquidity, quote, risk, and policy requirements. |
| `optedge/robinhood_mcp.py` | Implements official Robinhood MCP OAuth, OS-vault credential storage, and the narrow read/review policy. |
| `optedge/robinhood_connection.py` | Bridges the cockpit to one bounded private MCP event loop without polling, retries, or placement. |
| `optedge/robinhood_finalist.py` | Verifies the unchanged top option candidate against an exact, fresh Robinhood chain, instrument, and quote before planner promotion. |
| `optedge/evidence_capture.py` | Freezes one fresh, source-bound Robinhood finalist into an idempotent current-policy paper-evidence signal without another broker call. |
| `optedge/robinhood_option_history_sync.py` | Resolves and atomically caches a bounded batch of exact Robinhood option daily histories using read-only calls. |
| `optedge/robinhood_snapshot_sync.py` | Performs one complete account-scoped read and atomically persists only redacted broker state and the pseudonymous risk ledger. |
| `scripts/local_cockpit.py` | Serves the local swing-trading cockpit and Trade Desk interface. |
| `scripts/export_robinhood_agentic_queue.py` | Builds a bounded options research shortlist and exact-candidate inputs; it never creates a broker review packet or places an order. |
| `scripts/normalize_robinhood_broker_snapshot.py` | Converts ignored read-only broker captures into a safe, pseudonymous dashboard snapshot. |
| `dashboard/` | Generates the standalone interactive HTML research dashboard. |
| `reports/` | Builds validation reports, equity curves, factor evidence, and research summaries. |
| `telemetry/` | Records engine timing, health, and cache diagnostics. |
| `logs/` | Stores ignored local option/share/futures signal history and pricing-model observations. |
| `optedge/default_weights/` | Immutable shipped strategy weights used when trusted runtime weights are unavailable. |
| `data/` | Git-ignored local research, broker, model, and lifecycle state; never commit private contents. |
| `docs/` | Architecture, validation, data-source, risk, broker-workflow, and limitation references. |
| `tests/` | Regression and safety tests run locally and across Python 3.11-3.13 in CI. |
| `examples/` | Sanitized examples that demonstrate formats without private runtime state. |
| `pyproject.toml` | Canonical package metadata, dependencies, command entry point, and tool configuration. |
| `install.bat` / `install.sh` | Reproducible source-first setup for Windows and Linux/macOS. |
| `LICENSE` | MIT license terms for the source code. |

## Tests

Install the development extra and run the same full-suite commands used by CI:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check . --select E9,F63,F7,F82,F401,F841,B033,B007,F541
```

Individual `tests/test_*.py` files can still be run directly while developing, but a clean full `python -m pytest` run is the release check.

Passing tests means the documented behavior and safety invariants did not regress. It does not establish profitability, quote quality, brokerage eligibility, or production readiness.

## Documentation

- [Complete Project Map](docs/PROJECT_MAP.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Edge Lab methodology](docs/EDGE_LAB.md)
- [LEAPS Swing Profile](docs/LEAPS_SWING.md)
- [Validation](docs/VALIDATION.md)
- [Data Sources](docs/DATA_SOURCES.md)
- [Free Data Roadmap](docs/FREE_DATA_ROADMAP.md)
- [Risk Model](docs/RISK_MODEL.md)
- [Factor Library](docs/FACTOR_LIBRARY.md)
- [Third-Party Forward Testing](docs/THIRD_PARTY_FORWARD_TESTING.md)
- [Limitations](docs/LIMITATIONS.md)
- [Contributing](CONTRIBUTING.md)
- [Security Policy](SECURITY.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)

## Privacy and Local Data

Optedge is local-first, but local does not mean risk-free. Generated dashboards, logs, broker snapshots, research caches, position state, model files, and lookup reports may contain financially sensitive context.

- Keep secrets in `keys.py` or local environment variables; both standard secret paths are ignored by Git.
- Treat everything under `data/`, `logs/`, `telemetry/`, and `archive/` as private unless you have inspected and sanitized it. Treat the external pseudonymous account-equity ledger directory as private too: `OPTEDGE_STATE_DIR` when set, otherwise the per-user OS state path described above. Custom/test data directories keep their ledgers under `robinhood_account_equity_ledgers/`.
- A `.gitignore` rule prevents ordinary accidental staging; it is not encryption and cannot remove a secret that was already committed.
- Never paste Robinhood passwords, cookies, MFA codes, API tokens, full account numbers, or raw authenticated responses into source files, issues, logs, or review packets.
- Treat `OPTEDGE_CONTACT` as personal information. It is disclosed to the SEC in SEC request headers, not sent in Optedge's general non-SEC product identity, and should not be committed or pasted into public reports.
- Review `git status` and the staged diff before every push.
- Use `python archive.py --dry-run` before resetting a research experiment.

Optedge's normalized broker view uses pseudonymous account keys, but even pseudonymous holdings and buying-power data should remain private.

## Contributing

Contributions are welcome when they preserve inspectability and the fail-closed safety boundary.

Read the complete [contribution guide](CONTRIBUTING.md), [security policy](SECURITY.md), and [Code of Conduct](CODE_OF_CONDUCT.md) before opening a pull request.

1. Create a focused branch and install the development extra with `python -m pip install -e ".[dev]"`.
2. Add or update regression tests for behavior changes, especially evidence eligibility, exact contract identity, sizing, freshness, broker reconciliation, and packet construction.
3. Keep network-dependent tests mocked and deterministic.
4. Run the full test and lint commands above.
5. Describe what changed, why it is safe, what evidence supports it, and what remains unverified.

Do not promote a factor from look-ahead results, label modeled option marks as fills, weaken a blocker to make the dashboard look ready, add credential scraping, or introduce unattended order loops. A broker or evidence failure should produce a visible unavailable/blocked state, not a guessed value.

Commit messages should explain the complete logical change (for example, `Validate edge by independent entry day`), not act as per-file captions. The project layout and module documentation explain individual file purposes.

## Limitations

Optedge is a research and decision-support tool, not financial advice and not an autonomous trading system.

Signals require human review. Performance depends on data quality, actual fills, spreads, slippage, liquidity, borrow and assignment behavior, regime changes, news shocks, earnings gaps, taxes, and sample size. Free providers can be delayed, incomplete, inconsistent, or unavailable. Model scores, backtests, forward tests, confidence intervals, drawdown interlocks, risk caps, and broker previews are evidence or controls with uncertainty—not promises and never a guarantee of profit.

See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## License

MIT. See [LICENSE](LICENSE).

The license governs the software; it does not provide financial, legal, tax, investment, brokerage, or fiduciary services. You are responsible for every decision and order made with information from this project.

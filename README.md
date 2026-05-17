# Optedge — Free Self-Improving Quant Cockpit (v20.7)

## v20.7 patch (May 2026) — profitability fixes + position tracking + unit tests

Twelve targeted changes to address the systematic -11% forward-test P&L:

### HIGH IMPACT — fixing systematic optimism in the EV/Kelly math
1. **Realistic fill slippage** (config `FILL_SLIPPAGE_PCT = 0.04`, default 4%
   round-trip). Subtracted from predicted return BEFORE EV and Kelly compute.
   v20.6 was assuming you fill at mid both sides — that's why every signal
   looked tradable on paper but bled real cents on entry.
2. **Conservative Kelly avg_win prior**. `max(0.30, abs(pred) * 1.3)`
   replaces the previous `max(0.50, abs(pred) * 2)` which doubled the
   prediction before any realized data justified it. Switches to a less
   conservative prior once a realized win-rate is supplied.
3. **VADER → FinBERT blend on WSB text.** VADER alone reads "this thing is
   going to dump hard 🚀" as bullish. Sentiment engine now batches all
   collected posts/comments through FinBERT once at the end and blends:
   `final = 0.5 × finbert + 0.3 × vader + 0.2 × keyword`. Reuses the same
   FinBERT instance the news engine already loaded (GPU when available).
4. **Walk-forward validation guard.** Lasso refit of `config_runtime.py`
   now requires ≥500 logged signals AND ≥10 distinct trading days
   represented in entry_time. Previously a single weird day could overfit
   the weights for every future run.
5. **prob_win DTE discount.** A 0.10-delta 5-DTE call has 10% chance of
   landing ITM but a much lower chance of being a P&L winner after theta +
   spread. `prob_win = abs(delta) × max(0.5, dte / 30)` corrects this.

### MEDIUM IMPACT
6. **`net_edge_pct` in mispricing** — `|mispricing_pct| - spread_pct`. A 5%
   apparent mispricing on a 6%-spread contract is NOT tradable; this exposes
   the real edge net of bid-ask costs. New column on every contract.
7. **Position-level P&L tracking** (`backtest/positions.py`). Maintains
   `data/open_positions.json` (current view of every still-open recommendation
   with entry price + DTE) and `data/closed_positions.json` (realized exits
   via expiry / stop / target). Forward-test denominator is now correct:
   "still open and up 40%" is distinguishable from "expired worthless".
8. **Sector concentration cap.** Max 25% of bankroll (40% aggressive) in any
   single GICS sector across all option positions. Prevents 6 tech calls
   triggering at once and putting all eggs in one beta bucket.
9. **Per-engine SLA telemetry on dashboard.** Engine telemetry table now
   shows a relative latency bar + flags engines hitting ≥80% of their
   configured SLA in orange and SLA breaches in red. Easy to see where to
   add `--skip-X` to your daily command.
10. **Per-factor IC log enumeration.** The predictor now prints which
    factors were predictive (+IC > 0.02) vs anti-predictive (-IC < -0.02)
    after every refit — surfaces which engines are actually contributing.

### LOWER IMPACT
11. **Model weight history rolling log** (`data/model_weights_history.jsonl`).
    Each `model_accuracy.refit_weights()` call appends a timestamped
    snapshot. Capped at 1,000 rows. Lets the dashboard chart how the
    BS/CRR/BJS/CBOE ensemble has evolved over time — useful for spotting
    overfitting visually.
12. **Reddit stopwords expanded.** Added 50+ common false-positive tickers
    that the sentiment regex was matching (NEW, ALL, FOR, THE, ARE, BUY,
    NOW, GET, CAN, IRS, CPI, USD, EUR, etc.).
13. **Unit tests** (`tests/test_pricing.py`). 20 tests covering BS, CRR,
    BJS, IV round-trip, ensemble, regime classification, Kelly with
    slippage, DTE discount. Run with `python tests/test_pricing.py`.
14. **Stale Heston docstring** in `pricing_models.py` — fixed to reflect
    that Heston is disabled by default.

### Skipped intentionally
- **#12 Hedge executor** — per your request.
- **#13 Engines subdirectory refactor** and **#14 Config consolidation** —
  these are correct improvements but break every import path. NOT safe to
  drop on a live `--aggressive` $25K loop. Worth doing as a deliberate
  downtime patch later.

## v20.6 patch (May 2026) — FinBERT torch-2.5 safetensors fix

One-line follow-up to v20.5. The first GPU-aware FinBERT run hit this:

```
finbert: load failed (Due to a serious vulnerability issue in `torch.load`,
even with `weights_only=True`, we now require users to upgrade torch to at
least v2.6 in order to use the function. This version restriction does not
apply when loading files with safetensors.
See https://nvd.nist.gov/vuln/detail/CVE-2025-32434) — engine will return empty
```

ProsusAI/finbert is only published as `pytorch_model.bin`, and transformers
≥ 4.40 refuses to load `.bin` checkpoints unless torch ≥ 2.6. Per the user's
diagnose_gpu.py: they're on torch 2.5.1+cu121 (which works great for GPU,
just not for the legacy load path).

**Fix**: try multiple FinBERT variants in priority order, starting with
safetensors-compatible ones:

1. `yiyanghkust/finbert-tone`  (safetensors, 3-class financial sentiment)
2. `mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis`  (safetensors)
3. `ProsusAI/finbert`  (legacy .bin — final fallback for torch ≥ 2.6 users)

First one that loads wins. Per-model label index mapping handled so the
`finbert_score = P(positive) - P(negative)` calculation works regardless of
which model loaded. Load-failure is cached so we don't retry every iter
within the same `--loop` process.

If the user later upgrades to torch 2.6 (`pip install -U torch`), ProsusAI's
original FinBERT will become loadable too — no code change needed.

## v20.5 patch (May 2026) — smile-aware vol, FinBERT fixes, GPU diagnostics

Five surgical fixes off the v20.4 production run feedback:

1. **Smile-aware fair vol input.** v20.4 used a flat `HV30` as the input vol
   for BS/CRR/BJS. v20.5 uses the **per-expiry median market IV** from the
   live chain (computed from tight-spread, decent-OI contracts). This closes
   most of the gap with CBOE's surface-aware theo and makes our home-rolled
   models contribute real signal instead of just BS-ish constants. HV30 stays
   as the fallback when an expiry doesn't have enough quality contracts to
   form a robust median.

2. **Adaptive weight ceiling.** The first iter of v20.4 correctly identified
   that CBOE's `theo` is ~17x more accurate than BS/CRR/BJS at predicting
   current mid — and gave it **86% weight**. The ensemble became a CBOE
   rebroadcast and our independent models stopped contributing. v20.5 caps
   any single model at 55% weight with a 10% floor on every model. CBOE
   still wins on accuracy but the other three keep enough weight to express
   when they DISAGREE with the market — which is where mispricing alpha lives.

3. **FinBERT now reads `top_headline`.** v20.4 looked for `headline` / `title`,
   which the optedge news engine doesn't emit (it emits `top_headline`).
   `finbert=0 rows` even though the model loaded. v20.5 detects the right
   column.

4. **Detailed GPU diagnostics.** v20.4 logged `loaded (device=cpu)` with no
   explanation. v20.5 prints torch version, CUDA build version, device count,
   AND a specific reinstall command when CUDA isn't available — so the user
   can self-diagnose whether they installed the wrong torch wheel or have a
   driver-mismatch issue. Standalone script `diagnose_gpu.py` runs the same
   checks without launching the whole pipeline.

5. **BJS deep-ITM put fallback.** The Bjerksund-Stensland transform
   occasionally produced ~0 prices for deep-ITM puts at certain rate/dividend
   configurations. v20.5 detects this numerically and falls back to BS as a
   safety floor for those rows. Intrinsic floor always enforced.

Plus: cosmetic log bug fix — mispricing's "2 rows" was the dispatcher
counting dict keys instead of the contracts DataFrame.

### Why CBOE's theo isn't the whole answer (and 55% cap matters)

CBOE's `theo` is derived from their live IV surface fit. By construction
it's the closest thing to market mid. Optimizing the ensemble for
mid-prediction accuracy alone would always crown CBOE ~85%+ and make our
independent models cosmetic. But the **signal** is in the **DISAGREEMENT**,
not the consensus — when BS+CRR+BJS all agree but their consensus differs
from market mid (≈ CBOE theo), THAT'S the mispricing. So we cap CBOE at
55% to ensure independent opinions are still part of the ranking.

## v20.4 patch (May 2026) — vectorized pricing + whole-day reliability

### Vectorized pricing — every contract runs every model

The per-row Python loop in `_enrich_chain` was replaced with numpy-vectorized
calls in `pricing_models.py`:

| Model | Per-contract before | After (vectorized) | Speedup |
|---|---|---|---|
| Black-Scholes-Merton | ~100µs   | ~0.9µs  | 110x |
| CRR Binomial (80 steps) | ~660µs   | ~60µs   | 11x |
| Bjerksund-Stensland 2002 | ~6700µs | ~2µs    | 3000x |

Net: a 12-ticker mispricing run that took 28s in v20.3 now takes 6.8s in
v20.4 (sandbox numbers; residential is faster). **All four models run on
every contract** — no fast-path corner-cutting that v20.3 used for ATM
non-dividend calls. CBOE-provided Greeks (delta/gamma/theta/vega) are
captured alongside the BS-computed delta so the dashboard can show source
Greeks for cross-validation.

### Bumped worker counts — uses your CPU cores

`config.WORKERS_*` is now CPU-scaled:

| Engine | v20.3 | v20.4 (8-core) | v20.4 (16-core) |
|---|---|---|---|
| MISPRICING | 6 | 16 | 24 (capped) |
| FUNDAMENTALS | 8 | 12 | 24 |
| NEWS | 8 | 12 | 24 |
| EARNINGS | 6 | 10 | 15 |
| VALUE | 8 | 12 | 18 |

yfinance-sensitive engines (FUTURES, ANALYST) stay capped to respect the
old rate limits. The bumps target CBOE-backed engines and stdlib HTTP
that don't rate-limit at the same level.

### Whole-day reliability — `telemetry/health.py`

Each iter records a row in `telemetry/health.parquet` (or `.jsonl`
fallback if no parquet engine): iteration number, runtime, RSS memory,
cache size, error string (if any). At 30-min loops that's 13 rows per
trading day; rolling window caps at 500 rows.

Pre-iter check: if RSS > 6GB the harness forces a GC + prunes the cache
to half-size; if still > 8GB it logs a recommendation to restart. The cap
prevents the silent-degradation pattern where a long-running loop slowly
leaks until the dashboard renders crash.

### Adaptive ensemble keeps learning — even on the JSON path

`backtest/model_accuracy.py` now reads both `.parquet` AND `.json`
prediction logs, so the adaptive weights still update on environments
without pyarrow. mispricing's prediction logger also falls back to JSON
gracefully.

### Removed Heston (temporarily)

The Heston stochastic-volatility model was added in development but the
Lewis-2001 Fourier integration didn't reproduce known closed-form prices
within tolerance. It's been disabled until the calibration is right —
shipping incorrect pricing to a `--aggressive` $25K bankroll is worse
than waiting. The vectorized BS/CRR/BJS + CBOE quartet covers the
smile cases well enough for now (CRR handles American early-exercise,
CBOE's theo is implied-vol-aware).

## v20.3 patch (May 2026) — multi-model pricing ensemble + adaptive learning

### Multi-model options pricing ensemble (`pricing_models.py`)

mispricing.py now scores every contract with **four** pricing models and
combines them via a regime-aware weighted ensemble:

| Model | Type | When it shines |
|---|---|---|
| Black-Scholes-Merton | European closed-form | ATM, short DTE, low rates |
| Cox-Ross-Rubinstein binomial | American (early exercise) | Deep ITM puts, dividend calls |
| Bjerksund-Stensland 2002 | American closed-form | Fast approx to CRR |
| **CBOE proprietary `theo`** | Market-implied | Anchor / sanity-check |

Each contract gets `theo_bs`, `theo_crr`, `theo_bjs`, `theo_cboe` columns +
the ensemble `theo_price`. Source-provided Greeks (delta/gamma/theta/vega
from CBOE) are also captured per contract instead of always recomputed.

**Fast path**: for calls on near-zero-dividend stocks, BS = CRR = BJS
exactly (no early exercise benefit), so we short-circuit. The expensive
binomial only runs on puts and dividend calls.

**Pre-filter**: contracts that won't survive OI+volume thresholds are
dropped BEFORE the expensive pricing pass — 2-3x speedup on liquid tickers.

### Adaptive learning — every angle (`backtest/model_accuracy.py`)

Each iter writes `logs/model_predictions_YYYYMMDD_HHMMSS.parquet` containing
every model's predicted mid for every actionable contract. Each subsequent
iter, `model_accuracy.refit_weights()`:

1. Loads the last 14 days of predictions.
2. Re-fetches current mid for each contract via `chain_provider`.
3. Computes per-model mean absolute error, grouped by VIX regime
   (low/normal/high vol).
4. Updates `data/model_weights.json` with weights ∝ 1/MAE per regime.

mispricing.py reads those weights at the start of the next run. **The
ensemble auto-adjusts to favor whichever model best predicts realized mid
in each volatility regime.**

This is on top of the existing:
- Lasso refit on realized P&L → `config_runtime.py`
- IC backtest cache → `data/last_ic.parquet`
- Per-engine alpha decay → `backtest/alpha_decay.py`
- Drawdown circuit breaker → `backtest/drawdown_breaker.py`

### Fast direct history fetch (Yahoo v8)

`data_provider.get_history()` now hits `query1.finance.yahoo.com/v8/finance/chart`
directly via stdlib urllib (bypasses yfinance's heavyweight throttle which
was returning 0 rows in some sandbox tests). Falls back to yfinance library
if v8 fails. State-pack identified yfinance-throttled history as the main
HV-calc drag — this should resolve it.

### Optional GPU FinBERT sentiment (`engines/finbert.py`)

**Opt-in**, **graceful no-op** when dependencies missing. Defaults stay the
same; no change to `requirements.txt`. To enable:

```powershell
pip install transformers
# For CUDA acceleration on your GPU:
pip install torch --index-url https://download.pytorch.org/whl/cu121
# CPU-only (still ~3x slower than VADER but +30% accuracy):
pip install torch
```

Once installed the engine wakes up automatically: auto-detects CUDA, falls
back to CPU torch, scores headlines from the existing news engine. Emits
`finbert_score` per ticker which fusion treats as an additive factor next
to VADER (disagreements between the two are themselves informative).
Skip via `--skip-finbert`.

## v20.2 patch (May 2026) — empty-engine fixes + multi-source options chains

### NEW: Multi-source options chain provider (`chain_provider.py`)

**Problem in v20.1:** yfinance was rate-limited from residential IP, leaving
mispricing with only ~2 contracts per iter and the THIN CHAIN COUNT warning
firing every run.

**Fix:** new `chain_provider.fetch_chain()` layered three keyless sources:

  - **PRIMARY  : CBOE delayed quotes** (`cdn.cboe.com/api/global/delayed_quotes`)
    Returns ALL expirations + every strike + Greeks (delta/gamma/theta/vega)
    in a single HTTP call. Typical response ~200ms. Tested: 50 tickers in
    4.2s with 8 parallel workers. **No signup, no key.**
  - **FALLBACK 1: NASDAQ option-chain JSON** (`api.nasdaq.com/api/quote/{T}/option-chain`)
    Returns bid/ask/volume/OI per strike. Used when CBOE doesn't list the
    symbol (rare — niche small caps). Auto-tries stocks→etf→index asset
    classes. **No signup, no key.**
  - **FALLBACK 2: yfinance (legacy path)**
    Preserved as a final safety net so behavior degrades gracefully if both
    CBOE and NASDAQ are blocked.

`data_provider.get_options_chain()` delegates to the new provider; no engine
needed code changes. Sample result on AAPL: source=`cboe`, spot=$294.49,
27 expirations, 3,542 contracts. SPY: 13,576 contracts. **mispricing now
produces ~2,000+ contracts per iter** instead of 2.

### Existing engine fixes (layered API + no-key fallbacks)

Every API path from v20.1 is preserved unchanged. v20.2 adds a no-key fallback
**beneath** each one so the engine returns rows whether or not the user has
configured the relevant key. If the API works, you keep using the API.

- **CoT** — root cause: v20.1 used `c_disagg.txt` which is commodities-only,
  silently dropping 8 of the 14 markets (S&P 500, NASDAQ, Russell, T-Notes,
  EURO FX, BTC, etc.). Fix: PRIMARY = CFTC Socrata
  (`publicreporting.cftc.gov`) covering Disaggregated **and** TFF in one
  keyless feed. The legacy TXT stays as a fallback.
- **EIA** — root cause: required `EIA_API_KEY`, returned empty when not set.
  Fix: keeps the v2 API path; adds an HTML scrape of `ir.eia.gov/ngs/ngs.html`
  and `eia.gov/petroleum/supply/weekly/` as a no-key fallback. If you have
  set `EIA_API_KEY` you can drop `--skip-eia` from your daily command.
- **WASDE** — root cause: date logic treated day-of release (e.g. May 12) as
  last-month + 30 days old, dropping all rows. Fix: any day on/after the 8th
  counts as "this month's WASDE has occurred"; widened proximity window so
  ag exposure stays on the board between releases.
- **Google Trends** — root cause: `pytrends` either unavailable or
  rate-limited. Fix: keeps the pytrends primary path; adds a Wikipedia
  pageviews fallback (`wikimedia.org/api/rest_v1`) keyed by ticker company
  longName. Free, no key, no quota.
- **Form 144** — root cause: cache was poisoning itself with empty results
  for 12h after a transient EDGAR hiccup. Fix: don't cache empties; add a
  CIK→ticker fallback via `sec.gov/files/company_tickers.json` for filings
  whose `display_names` omit the parenthesized ticker.
- **Whisper** — root cause: required Finnhub key; engine disabled silently
  otherwise. Fix: keeps Finnhub as primary; adds yfinance Ticker.info
  `targetMeanPrice` fallback (free, no key, already in your hardened session).
- **FDA calendar** — root cause: BiopharmCatalyst is now Cloudflare-protected
  from at least some IPs; the engine had no fallback. Fix: keeps the three
  existing scrapes (BPC, RTTNews, Drugs.com) and **merges** in two new
  keyless sources — openFDA recent supplements (`api.fda.gov`) and SEC EDGAR
  8-K full-text "PDUFA" mentions. Per-ticker we keep the soonest catalyst.
- **Short interest** — root cause: `data_provider.get_fundamentals` cache
  intentionally excludes short-interest fields, so the engine was reading
  `None` for every value and emitting 0 rows. Fix: added
  `data_provider.get_short_info()` which pulls and caches the short fields
  separately. Added FINRA RegSHO daily short-volume file as an amplifier
  (covers ~11K symbols, no key).

Migration: extract `optedge_v20_2.zip` OVER your existing folder. State files
survive (`config_runtime.py`, `data/predictor_coefs.json`,
`data/last_ic.parquet`, `logs/*.parquet`, `data/_cache/`, `keys.py`). The
fundamentals cache schema is unchanged — your existing cached fundamentals
files remain valid.

## v20.1 patch (May 2026) — bug fixes from first live v20 run

- **CoT** — switched to current-week TXT file at cftc.gov/dea/newcot/c_disagg.txt (no zip parsing). 7 markets now firing correctly.
- **13F** — swapped fragile Atom feed for data.sec.gov/submissions JSON. 12 smart-money funds tracked (Berkshire, Tepper, Burry, Ackman, Renaissance, Bridgewater, Citadel, Two Sigma, Coatue, Soros, Tiger Global, Light Street).
- **EIA** — v1 API was deprecated; now uses v2 with a free key. **Register at https://www.eia.gov/opendata/register.php** and add `EIA_API_KEY = "your-key"` to `keys.py`.
- **Buybacks + Form 144** — fixed ticker parsing (EFTS embeds tickers in `display_names`, not `tickers` field).
- **Whisper** — earningswhispers.com is JS-rendered now; engine pivoted to use Finnhub analyst price-target gap as a proxy (works with your existing Finnhub key, zero extra network).
- **Twitter** — Nitter mirrors were all down; engine now uses Apewisdom (aggregates Twitter+Reddit+StockTwits mentions). Falls back to Nitter if Apewisdom is down.
- **Credit spread** — fixed bug: FRED returns values in PERCENT not BASIS POINTS. Display + threshold math now correct (real HY OAS ~280-380bp).
- **Pandas warnings** — silenced 50+ PerformanceWarnings via 3 strategic `df.copy()` calls in fusion.
- **Empty-engine diagnostic** — new dashboard panel lists which engines returned 0 rows + the likely cause.
- **Per-engine skip flags** — every v20 engine now has its own `--skip-X` (e.g. `--skip-cot --skip-13f`).
- **Thin-chains warning** — if mispricing returns under 50 contracts, run.py prints the yfinance rate-limit message explaining why.



Multi-factor screener that ranks **long calls**, **long puts**, **small-cap shares**, **value plays**, and **futures** using free public data. Auto-retrains weights from realized P&L. Sizes positions via Kelly. Auto-opens an interactive dashboard. **Run all day with `--loop`.**

## What's new in v20 (May 2026)

**37 fusion factors** (was 22). Five-tier upgrade pack:

- **Tier A — Latency:** Universe pre-filter (top 300 by mcap + WSB + priors for slow per-ticker engines), per-engine SLA timeouts (one slow source no longer blocks the iter), telemetry tracking, HTTP/2 via `httpx`, optional async fan-out via `aiohttp` (`async_http.py` helper). Engine timings now visible on the dashboard.
- **Tier B — 10 new free data engines:** CFTC Commitments of Traders, SEC 13F smart-money deltas (Berkshire/Tepper/Burry/Ackman/etc.), CBOE VIX futures term structure, EIA petroleum + natgas weekly inventories, USDA WASDE ag reports, SEC 8-K buyback announcements, Google Trends search momentum, SEC Form 144 pre-sale notices (bearish leading indicator), earningswhispers.com whisper-vs-consensus, Hyperliquid decentralized perpetuals OI + funding.
- **Tier C — Quality upgrades:** Twitter/X cashtag sentiment via Nitter mirrors, r/options daily-discussion deep scan, Form 4 cluster-buy detector (3+ insiders within 14d).
- **Tier D — Risk & portfolio:** Treasury yield curve PCA factors (level/slope/curvature → banks/insurers/duration), IG/HY credit spread divergence (cyclical credit stress), portfolio Greek aggregation panel (net Δ/Γ/Θ/V), hedge suggestion when net delta > $5K, drawdown circuit breaker (auto-halves Kelly on rolling -10% P&L).
- **Tier E — Telemetry:** Per-engine latency table, cache hit-rate tracking, alpha-decay tracker (`backtest/alpha_decay.py`).

Migration: extract `optedge_v20.zip` OVER your existing `optedge/` folder. The state files survive (`config_runtime.py`, `data/predictor_coefs.json`, `data/last_ic.parquet`, `logs/*.parquet`, `data/_cache/`, `keys.py`).

## ⚡ One-time setup (Windows)

Double-click **`install.bat`**. It auto-detects Python 3.13/3.12 (refuses 3.14 since wheels aren't ready), creates a venv, installs deps, runs setup_check.

Or manually:
```bash
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
python setup_check.py
```

## ⚡ Daily use

```bash
# Single run, auto-opens dashboard
python run.py

# AGGRESSIVE mode — ½ Kelly, 10% per option / 15% per share caps
python run.py --aggressive

# Run all day — auto-refresh every 30 minutes
python run.py --aggressive --bankroll 25000 --loop 30

# Continuous + skip slow engines (fastest loop)
python run.py --loop 15 --fast-insider --skip-wsb --aggressive
```

In loop mode, the dashboard opens once on iteration 1 and refreshes every cycle (just reload the browser tab). Logs accumulate in `logs/` so the auto-retrain gets smarter every day. Ctrl+C anytime to stop.

## What you get on every run

| Section | What's there |
|---|---|
| **Macro banner** | Regime (risk-on / off / neutral), VIX, 10Y-3M slope, SPY 3M |
| **Stats panel** | Run time, universe size, idea counts |
| **News flow** | Top headlines by velocity × Δsentiment |
| **Earnings calendar** | Next 14 days of earnings + last surprise |
| **Performance Tracking** | Win rate, avg P&L, by-confidence stats from prior runs |
| **Insider heatmap** | Top buyers / sellers from real SEC EDGAR data |
| **WSB trending** | Live-discovered tickers from Reddit |
| **Long Calls** | Cheap calls aligned with bullish multi-factor stack |
| **Long Puts** | Cheap puts aligned with bearish multi-factor stack |
| **Long Shares** | Small-caps where options aren't liquid |
| **💎 Value Plays** | Magic Formula + Graham composite cheap & quality |
| **📈 Futures** | /ES /NQ /GC /CL /BTC etc. with momentum + range pos |
| **Ranked tables** | Full snapshots, all ideas |
| **TradingView watchlist** | Importable file |
| **Methodology appendix** | All weights + thresholds explained |

Every card shows:
- Confidence (0-100, fused 10-factor score)
- Predicted % return (from auto-retrained Lasso predictor)
- EV % (predictor's expected value)
- ¼ or ½ Kelly % of bankroll
- Suggested $ allocation + contract count
- 🛑 Stop trigger / 🎯 Target trigger
- Why (multi-factor reasoning) + Risks

## Interactive controls

The sticky control bar lets you in real-time:
- 🔍 **Search** — type a ticker, only its cards show
- **Sort** — by rank / confidence / predicted / EV / Kelly / ticker A-Z
- **Filter chips** — calls only, puts only, shares only, conf ≥ 70, EV > 0, Kelly > 0
- **Live counter** shows visible-card count

## Self-improvement loop

```
Daily run → logs signals → forward test re-prices old signals → Lasso refit on
realized P&L → updates config_runtime.py (overrides default weights) →
predictor coefs updated → next run uses fresh weights and predicted returns
```

After 30 days of `--loop` running you'll have **~10,000+ logged signals** and the Lasso refit will replace the IC-bootstrap with actual P&L-derived weights.

## Free data sources

| Engine | Source | Auth |
|---|---|---|
| Options chains, prices, fundamentals, futures | yfinance | none |
| Sentiment + WSB trending | Reddit public JSON | none |
| Insider | SEC EDGAR Form 4 | none |
| News | Google News RSS | none |
| Macro | yfinance + optional FRED | FRED key optional, free |

## Sizing math (the important part)

**Default** (¼ Kelly): conservative, ~75% of long-run growth with half the drawdown.
- Per-option cap: 5% of bankroll
- Per-share cap: 8% of bankroll
- Total options cap: 30% of bankroll

**Aggressive** (½ Kelly with `--aggressive`): faster compounding, ~3x bigger drawdowns.
- Per-option cap: 10% of bankroll
- Per-share cap: 15% of bankroll
- Total options cap: 60% of bankroll

Negative Kelly = predictor disagrees with rank → marked as **skip** on the dashboard. Don't override.

## Exit triggers

Built into every card so you have a complete plan before entering:

**Options:**
- 🛑 Stop = 50% of entry price (max 50% loss)
- 🎯 Target = 200% of entry price (double = exit half, let rest run)

**Shares:**
- 🛑 Stop = -8% (default) / -10% (aggressive)
- 🎯 Target = +20% (default) / +30% (aggressive)

These are starting heuristics — adjust in `backtest/sizing.py` to match your style.

## Common workflows

```bash
# Pure overnight setup: kick this off and walk away
python run.py --loop 60 --aggressive --bankroll 20000

# Pre-market scan (fast)
python run.py --fast-insider --skip-wsb --skip-news

# After-hours research mode (full universe, no rush)
python run.py --max-calls 30 --max-puts 20

# One-time IC backtest (run once a week)
python run.py --backtest

# Forward test summary (after several days of logs)
python run.py --forward
```

## Troubleshooting

### `python run.py` exits silently

Make sure `run.py` is the v10 version — first thing it prints is "Optedge starting…". If you see nothing, re-extract from the v10 zip.

### Yahoo rate-limit (429)

Common from datacenter / VPN IPs. Wait 15-30 min, ensure you're not on a VPN, or use `--demo` while you sort it.

### Reddit 403

System auto-skips sentiment when blocked. No action needed.

### Loop mode opens 50 browser tabs

Bug — fixed in v10. Each loop iteration sets `OPTEDGE_NO_OPEN=1` after the first open.

### Want to revert auto-retrained weights

Delete `config_runtime.py`. The system will recreate it next run.

## Architecture

```
optedge/
├── install.bat / install.sh   ← one-click setup
├── setup_check.py             ← health check
├── config.py                  ← universe, weights, filters
├── utils.py                   ← BS pricing, NaN-safe primitives
├── data_provider.py           ← curl_cffi wrapper + disk cache
├── run.py                     ← orchestrator (with --loop)
├── demo_data.py               ← synthetic fallback
├── engines/
│   ├── mispricing.py          ← BS + Brent IV solver
│   ├── sentiment.py           ← Reddit + VADER
│   ├── fundamentals.py        ← yfinance fundamentals
│   ├── insider.py             ← SEC EDGAR Form 4
│   ├── macro.py               ← VIX + yields + regime
│   ├── wsb_trending.py        ← live ticker discovery
│   ├── news.py                ← Google News RSS + VADER
│   ├── earnings.py            ← calendar + EPS surprise
│   ├── value.py               ← Magic Formula + Graham
│   └── futures.py             ← /ES /NQ /GC /CL /BTC etc.
├── fusion/
│   └── rank.py                ← 10-factor weighted fusion
├── backtest/
│   ├── track.py               ← signal log + LassoCV retrain
│   ├── forward.py             ← replay logged signals → realized P&L
│   ├── historical.py          ← IC analysis
│   ├── predictor.py           ← Lasso predictor + auto-retrain
│   └── sizing.py              ← EV + Kelly + exit triggers
└── dashboard/
    └── build.py               ← interactive HTML cockpit
```

## Philosophy

- **Long-only**: cheap directional optionality + multi-factor confirmation
- **Confluence over single signals**: 10 factors must roughly agree
- **Diversity**: max one option idea per ticker
- **Self-improvement**: weights + predictions retrain from your own log
- **Explainable**: every trade has reasoning + risks + exit triggers
- **Free**: every data source is free, no paywalls
- **Aggressive optional**: ½ Kelly when you want it

Not investment advice. Trade options at your own risk.

<!-- Purpose: Define the Edge Lab evidence lanes, calculations, and live-review gate. -->

# Edge Lab Methodology

Edge Lab answers one narrow question:

> Does this asset's current swing method have enough independent, after-cost, stable evidence to be eligible for a manual live-capital review?

It does not predict the next trade, guarantee profit, replace portfolio judgment, or authorize an order. Every requirement is conjunctive: one failed requirement keeps the asset blocked.

## Inputs and Lineage

Edge Lab reads:

- `data/fixed_horizon_outcomes.parquet` for one-, three-, five-, ten-, and twenty-session forward outcomes.
- `data/fixed_horizon_summary.json` for evidence provenance, outcome-set integrity, mature-resolution coverage, and the fixed-horizon report timestamp.
- `data/validation_summary.json` for the configured headline horizon when the fixed-horizon summary does not provide it.

The outcome file and fixed-horizon summary must each be no more than 96 hours old. The summary's own `generated_at` value must also be within that window and cannot be more than one hour in the future. The 96-hour allowance safely spans a normal market weekend without accepting indefinitely stale evidence.

Every new signal is stamped at log time with the exact provenance schema, strategy version, fixed-horizon methodology version, policy digest, model-trust schema and status, active predictor SHA-256 identity, active runtime-weight SHA-256 identity, option-adaptation status, and experiment ID. Those values carry into its outcomes, and the experiment identity binds the strategy, evidence policy, and active models. The fixed-horizon summary must match the current provenance, and its SHA-256 outcome-set digest must match the parquet contents. Missing, stale, malformed, mismatched, duplicated, or unattested evidence fails closed.

Historical evidence is never silently upgraded. Rows without the exact current provenance remain visible as `legacy_research_only`, but cannot authorize live review.

Run the evidence pipeline with:

```bash
python run.py --validation-report
```

Then open the Trade Desk:

```bash
python run.py --cockpit
```

The cockpit exposes the same payload at the local read-only endpoint `/api/edge-lab`.

## Independence and Overlapping Horizons

Repeated signals are not automatically independent observations. Fixed-horizon validation keeps the first thesis for each asset, symbol, direction, and UTC entry day. Edge Lab then groups eligible after-cost outcomes into chronological entry-day averages.

If ten tickers trigger on the same macro event on the same day, they still represent one entry-day environment for uncertainty estimation. The dashboard reports `signals_per_entry_day` so clustering remains visible.

The confidence interval uses a deterministic circular moving-block bootstrap over those entry-day averages. Block length is at least the holding horizon. This prevents overlapping ten-session outcomes from being treated like independent one-day observations.

`effective_horizon_blocks` is:

```text
floor(distinct entry days / horizon sessions)
```

The live gate requires at least 30 effective horizon-length blocks. At the default ten-session headline horizon, that requires about 300 distinct entry days. A large row count or many signals on the same days cannot replace that time coverage. At another horizon, the corresponding implication is `30 x horizon sessions` distinct entry days.

## Evidence Lanes

For each asset and horizon, Edge Lab chooses the first non-empty lane:

1. `current_method_executable`
2. `current_method_shadow`
3. `legacy_research_only`

The lanes are never blended.

### Current-method executable

Rows bound to the exact current evidence policy that passed the independent and executable-evidence rules. This is the only lane that can clear the live-capital evidence gate.

### Current-method shadow

Rows whose current strategy qualification and intended size were frozen before portfolio-level guardrails. They can accumulate forward research evidence without pretending that a blocked or zero-size recommendation was executed. Shadow evidence cannot authorize live review.

### Legacy research only

Unstamped, older, or policy-incompatible rows retained for auditability. Even a large profitable legacy sample remains blocked from live eligibility.

## Headline Horizon and Resolution

The default headline is ten completed market sessions. Edge Lab also reports five- and twenty-session slices to show whether results depend on one holding period.

An outcome does not enter the mature resolution denominator until its required completed session can exist. For the gated mature cohort, resolution coverage must be exactly 100%: every expected outcome scored, no excluded outcome, no pending outcome, and the summary counts must reconcile with the persisted rows. The current partial session is never scored.

## Calculations and Data Completeness

For each asset, horizon, and lane, Edge Lab reports:

| Metric | Definition |
|---|---|
| Outcomes | Rows with a finite after-cost result and valid entry time. |
| Entry days | Distinct entry dates represented by those rows. |
| Effective horizon blocks | Complete horizon-length groups available from the entry-day series. |
| Win rate | Share of after-cost outcomes greater than zero. |
| Average / median return | Return after the row's recorded slippage assumption. |
| Profit factor | Positive after-cost returns divided by the absolute value of negative after-cost returns; an all-winner cohort is labeled explicitly as having no losses. |
| 90% moving-block interval | Deterministic 2,000-sample circular moving-block bootstrap interval over entry-day averages, using a block at least as long as the horizon. |
| SPY excess | Stored outcome return in excess of SPY over the same horizon. |
| `1.5x` and `2x` cost stress | Raw return less 1.5 or 2 times the recorded slippage assumption. |
| Option entry-spread coverage | Share of rows with a finite nonnegative signal-time spread. |
| Option cost-covers-spread coverage | Share of rows whose recorded cost is at least the signal-time spread. |
| First half / recent half | Entry-day averages before and after the chronological midpoint. |
| Broker-observed coverage | Share of selected current option outcomes labeled `broker_market_observed`. |
| Modeled-proxy coverage | Share labeled `modeled_option_proxy`, reported for research only. |

The bootstrap seed is fixed, so identical input produces an identical interval. Determinism helps audit and testing; it does not remove statistical uncertainty.

Every gated row must have a valid entry time and finite raw return, after-cost return, nonnegative slippage assumption, and SPY excess return. After-cost return must reconcile to `raw return - recorded slippage` within the fixed numerical tolerance. Each of these coverage checks must equal 100%; missing costs or benchmarks are never replaced with zero.

For options, fixed-horizon evaluation records the greater of the configured option-cost floor and the entry spread. A missing entry spread is labeled `configured_floor_missing_entry_spread`; it is not guessed. The live option gate additionally requires 100% finite nonnegative entry-spread coverage and 100% cost-covers-entry-spread coverage, so a floor-only fallback remains research-visible but cannot silently clear the gate.

Outcome IDs must be present and globally unique. The combination of independent key and horizon must also be unique among rows marked independent. Integrity failures return a structured unavailable result rather than scoring a partial dataset.

## Live-Review Requirements

An asset is `validated` only when every applicable requirement passes at the headline horizon:

| Requirement | Rule |
|---|---:|
| Evidence source | Outcome parquet and fixed summary are policy-bound and `<= 96h` old |
| Provenance | Exact current schema, strategy, methodology, policy digest, model-trust state, active predictor/weight identities, option-adaptation state, and experiment ID |
| Outcome-set integrity | Summary digest matches the outcome parquet |
| Evidence lane | `current_method_executable` |
| Mature resolution | `100%` scored, with `0` excluded or pending |
| Required-field coverage | `100%` entry-time, raw, after-cost, nonnegative-slippage, reconciliation, and SPY coverage |
| Independent outcomes | `>= 200` |
| Distinct entry days | `>= 30` |
| Effective horizon blocks | `>= 30`, with block length at least the horizon |
| Ten-session time coverage | About `300` distinct entry days (`30 x 10`) |
| Average return after costs | `> 0` |
| 90% horizon-length moving-block lower bound | `> 0` |
| Profit factor after costs | `>= 1.15` |
| Average SPY excess | `> 0` |
| Average return at doubled costs | `> 0` |
| First-half entry-day average | `> 0` |
| Recent-half entry-day average | `> 0` |
| Option entry-spread coverage | `100%` finite and nonnegative |
| Option cost coverage | `100%` of recorded cost assumptions cover the entry spread |
| Option performance basis | Broker-market-observed outcomes only |
| Broker-observed option coverage | `>= 50%` of the selected current option cohort |

The 200-outcome and 30-entry-day rules still apply, but they are only baseline floors. The ten-session time-coverage row states the stricter implication of the effective-block rule at the default headline horizon; raw row count cannot substitute for the required time span. The manual-review gate applies only to the planned asset. A validated share lane cannot authorize an option, and a validated option lane cannot authorize a futures order.

## Status Semantics

### Validated

Every live-review requirement for that asset passed. This only makes the evidence eligible to proceed to the remaining research, portfolio, freshness, instrument, broker, liquidity, and explicit-confirmation gates.

### Promising

The research sample has at least 50 outcomes across 10 entry days, a positive after-cost average, positive moving-block lower bound, profit factor of at least 1.10, positive doubled-cost result, and positive recent-half average. Because the lane or full live requirements still fail, it remains paper-only.

### Adverse

The research sample has at least 50 outcomes across 10 entry days and shows a non-positive average return, profit factor below 1.0, or a 90% interval whose upper bound is below zero.

### Fragile

The research sample exists, but is neither consistently promising nor clearly adverse. Mixed evidence remains paper-only.

### Insufficient

The research display has fewer than 50 outcomes or fewer than 10 distinct entry days. Repeated signals on the same days do not cure the independence shortfall.

### Unavailable

The fixed-horizon source is missing, unreadable, malformed, or has no independent scored outcomes. Unavailable is a blocker, never a neutral state.

### Blocked source

The evidence files exist, but freshness, provenance, digest, required-field coverage, or resolution attestation failed. Metrics can remain visible for diagnosis, but no asset can become live-capital eligible until refreshed evidence passes every source check.

## Option Outcome Quality

Free historical option data is incomplete. Optedge distinguishes:

- `broker_market_observed`: an exact, non-interpolated target-date Robinhood option trade bar supplied through the read-only connector cache.
- `modeled_option_proxy`: a constant-entry-IV model mark used when no exact bar is available.

All option performance gates use the `broker_market_observed` cohort alone. Modeled proxies remain visible in the research-only aggregate, but their returns, profit factor, stability, and sample size cannot improve live eligibility. The broker-observed cohort must also represent at least 50% of the selected current option cohort, and its rows must pass the complete entry-spread and cost-coverage checks above.

Neither label proves that Optedge received an executable fill. Last-trade bars can differ materially from contemporaneous bid/ask prices, and a modeled mark can miss volatility-surface changes, early exercise behavior, halts, and liquidity.

## Evidence That Cannot Promote the Model

`python run.py --backtest` runs the historical IC diagnostic. It compares current factor scores with returns that have already occurred, which is direct look-ahead. Its output is labeled `diagnostic_only_lookahead` and `eligible_for_model_promotion=false`.

The legacy current-mark model-accuracy refit is disabled by default. Variable-age current marks are monitoring telemetry, not fixed-horizon forecast labels.

Use chronological, frozen, exact-current-policy fixed-session evidence for promotion decisions. Calibration uses after-slippage outcomes and asset-appropriate prediction columns; mixed assets are not evaluated against one generic prediction field.

## Active-Model Identity

Normal scans are inference-only. Research fits are `shadow_untrusted` and cannot become active merely because they were saved. The safe fallback is the source-controlled factor weights plus a zero stock-return predictor. The stock predictor accepts share targets only, and option adaptation remains disabled until a separate path has direct broker-observed targets and passes purged out-of-sample promotion requirements.

Current evidence is bound to the exact ranking state actually used. `active_predictor_digest_sha256` identifies either the trusted share-predictor champion or the deterministic zero-predictor fallback. `active_runtime_weights_digest_sha256` identifies either the trusted runtime-weight champion or the deterministic source-controlled weight set. `model_trust_status` must be `source_controlled_defaults` or `trusted_champion_active`, and `option_adaptation_status` must match the disabled-pending-direct-OOS policy. A mismatch moves the row out of the current lane rather than borrowing performance from another model identity.

## How to Read the Dashboard

Review these fields in order:

1. **Source attestation** — verify the fixed-horizon artifacts are fresh, policy-bound, and digest-matched.
2. **Evidence lane** — require current-method executable; shadow and legacy lanes are research-only.
3. **Resolution and field coverage** — require complete scored outcomes, costs, reconciliation, and SPY comparison; options also need complete entry-spread and cost-covers-spread coverage.
4. **Effective horizon blocks** — distinguish real time diversity from a large correlated row count; ten sessions require about 300 entry days.
5. **After-cost average and profit factor** — confirm nominal performance is positive.
6. **90% lower bound** — check whether horizon-length moving-block uncertainty remains above zero.
7. **2x costs** — test sensitivity to optimistic slippage assumptions.
8. **Recent half and SPY excess** — identify deterioration and broad-market dependence.
9. **Option metric basis** — confirm option performance comes from broker-observed outcomes, not modeled proxies.
10. **Primary blocker** — treat the first failed requirement as binding.

Do not average away a failed requirement. The gate is conjunctive by design.

## Known Limitations

- A 90% interval still allows substantial uncertainty and is not a prediction interval for the next trade.
- Horizon-length circular blocking reduces dependence from overlapping outcomes, but sectors, regimes, shared factors, and longer-memory effects can remain correlated.
- SPY is not the correct benchmark for every asset or direction.
- Slippage stress scales the stored assumption; it cannot reproduce a missing order book or market gap.
- Using the signal-time spread as the option cost floor is conservative relative to ignoring the spread, but it does not reconstruct the exit spread, queue position, or actual fill path.
- First-versus-recent halves are a stability screen, not a full walk-forward parameter study.
- Observed option last trades are not bid/ask fills.
- Surviving every threshold does not account for taxes, assignment, exercise, outages, rejected orders, or individual suitability.

Edge Lab should become stricter as evidence and execution-quality data improve. Thresholds must not be relaxed merely to make a lane pass.

<!-- Purpose: Define position-sizing and portfolio-risk controls. -->

# Risk Model

Optedge is designed as a research and decision-support system, not an automatic execution engine.

## Sizing

The sizing layer uses:

- Expected value after estimated fill slippage.
- Fractional Kelly sizing.
- Per-trade caps for options and shares.
- Risk-budget sizing for futures using point value, ATR-like distance, and micro-contract preference.
- Sector concentration caps.
- DTE-aware option win-probability discounts.
- Earnings IV-crush discounts.
- Time-of-day liquidity discounts.

The Trade Desk adds a separate deterministic sizing layer for manual review. It does not use conviction or Kelly to increase size. The general planner rejects per-trade risk above `2%` of account equity and a total-open allocation fraction above `25%`; the Robinhood manual-review boundary is stricter and starts from a `1%` risk ceiling that the account-loss firewall may only reduce.

- `risk budget = account equity x risk per trade`
- `proposal sizing ceiling = min(account equity x total-open allocation fraction, available buying power when supplied)`
- `shares = floor(min(risk budget / planned stop loss per share, proposal sizing ceiling / entry price))`
- `contracts = floor(min(risk budget / full debit per contract, proposal sizing ceiling / full debit per contract))`

For shares, planned stop loss per share is the entry-to-stop distance plus round-trip slippage. For long options, the full debit, not the planned premium stop, is the risk-budget basis. This prevents a stop assumption from permitting more contracts than the account-level risk budget can absorb if the option loses its entire value. Round-trip slippage is still added to the planned stop loss and removed from planned reward.

Historical option evidence uses a separate conservative cost rule. Each fixed-horizon outcome records the greater of the configured option-cost floor and the entry spread. If the entry spread is missing, the configured floor is labeled as a fallback rather than presented as observed cost; that row cannot satisfy Edge Lab's option requirement for complete entry-spread coverage. The live option evidence gate requires every selected row to have a finite nonnegative entry spread and a recorded cost at least as large as that spread.

The normal option swing lane keeps its `90+` DTE default. `leaps_swing` must be selected explicitly and accepts only `365-900` DTE equity/ETF calls or puts. It uses a 25% premium-loss planning reference, a 35% premium-target reference, 3/5/10-session thesis reviews, and a 20-session maximum planned hold. Its evidence cannot come from the general option lane: every 5-, 10-, and 20-session slice must independently pass the profile-specific `option_leaps_swing` gate using broker-observed outcomes only. These references discipline sizing and review; they do not guarantee an exit price or profitable trade.

The planner keeps stop-risk and maximum capital-loss measures separate:

- A long share's planned stop loss is quantity times the entry-to-stop distance plus slippage. Its maximum capital-loss reference is the full entry notional.
- A long option's planned stop loss is quantity times the entry-to-stop premium distance, the standard `100x` multiplier, and slippage. Its maximum capital-loss reference is the full debit.
- A short share's capital loss is unbounded, so the current Robinhood handoff blocks short-share execution even though research sizing can still be calculated.

Stops are not guaranteed fills. A gap, trading halt, liquidity failure, or option expiry can produce a loss greater than the planned stop loss. Missing entry, stop, target, multiplier, or account limits makes size unavailable rather than silently treating the value as zero.

The proposal sizing ceiling bounds the new order before broker exposure is known. It is not the final portfolio check. The final gate also accounts for capital already at risk in the same broker account.

## Same-Account Total-Open Portfolio Gate

For each otherwise eligible account, `risk/portfolio.py` evaluates only the normalized broker rows carrying that exact pseudonymous `account_key`:

For the v2 manual boundary, that key uses the exact attested derivation `acct_ + first_16_lowercase_hex(SHA256(UTF8("optedge-robinhood-account-v1|" + strip(get_accounts.account_number))))`. Its schema is `optedge_robinhood_account_key_derivation_v1`; the normalized snapshot and review packet never persist the raw account number, although the private ignored raw capture necessarily contains it for scoped broker reads. The `...last4` account mask is display-only and can collide across accounts; eligibility, portfolio, drawdown, positions, and orders join on the full derived key, never the mask.

- Existing long shares reconcile absolute market value against absolute quantity times a valid current price. A materially conflicting pair blocks review instead of choosing the smaller exposure; when only one complete basis is available, that basis is used.
- Existing long options contribute whole-contract quantity times `100` times the highest valid ask, mark, or current price.
- The proposed share contributes full entry notional, not its smaller planned stop loss.
- The proposed long option contributes full debit.
- The capacity basis is the lower of the planner's assumed equity and that same account's live `portfolio.total_value`.
- `post-trade capital at risk = current same-account broker capital at risk + proposed capital at risk`.
- `total-open allocation cap = equity basis x allocation fraction`.

The proposal passes this layer only when post-trade capital at risk is no greater than the total-open allocation cap. Conservative buying power remains a separate affordability constraint on the new order; it does not increase the portfolio cap.

The exposure calculation fails closed when a position has an invalid or contradictory quantity alias, a duplicate/blank account identity, conflicting valuation, a short or boxed state, missing account scope, nonzero expired quantity, missing usable price, or any other ambiguity. It also blocks malformed pending assignment/exercise/expiry fields or transitions, nonstandard option multipliers, malformed contract identity, and any nonzero same-account nonterminal order because exposure may be changing. The separate trade-plan layer blocks adjusted option deliverables. Zero positions, terminal orders, and positions from other accounts do not consume this account's cap.

Research lifecycle recommendations and Agentic paper rows are not counted as broker capital. Only fresh normalized same-account broker positions can establish existing live exposure. Broker-linked local lifecycle rows are used for reconciliation, not as a substitute source for portfolio math.

The entry gate also blocks same-underlying exposure across supported asset types. An existing share position in a symbol blocks a new option entry on that symbol, and an existing option position blocks a new share entry. This conservative rule prevents the narrow entry flow from treating economically related share and option exposure as independent capacity; it does not claim to be a complete portfolio-correlation model.

## Robinhood Account Drawdown Interlock

Every explicit direct **Sync broker snapshot once** action, or explicit non-dry-run normalization of a valid manual bundle, appends one observation per eligible pseudonymous account to a durable local ledger. Direct sync calls only allowlisted account reads, follows at most 50 proven cursor-linked pages per collection, keeps raw account numbers in memory, and atomically persists only the redacted snapshot after every required account scope succeeds. It performs no order review or placement and never retries automatically. When the normalizer uses the repository's real `data/` directory, `OPTEDGE_STATE_DIR` selects the ledger directory explicitly; otherwise Windows uses `%LOCALAPPDATA%\Optedge\risk`, and Unix-like systems use `$XDG_STATE_HOME/optedge/risk` or the normal `~/.local/state/optedge/risk` fallback. An explicit custom or test data directory remains self-contained at `<data_dir>/robinhood_account_equity_ledgers/`.

Each observation binds the account key, broker source time, `portfolio.total_value`, normalized-source digest, previous observation hash, and its own SHA-256 hash. The ledger has a separate digest and is single-account by construction. An identical snapshot is deduplicated; a contradictory equal timestamp, backwards timestamp, broken hash, or account change is rejected rather than repaired. Each individual replacement is atomic. During an append, the normalizer first protects the prior chain, replaces the primary, then advances `account_<digest>.json.bak` to the same newest chain. A successful update therefore ends with identical newest primary and sidecar histories. A missing primary or required sidecar, a sidecar that is not a valid prefix, or a sidecar that lags the primary blocks manual review as possible deletion, rollback, divergence, or an interrupted final write. Explicit normalization can reseal a validated lagging sidecar to the already-valid primary without inventing an observation. The normalizer neither silently reconstructs lost history nor automatically rebaselines.

Manual review requires at least two strictly ordered observations, at least `18` hours from the first observation to the current tail, coverage of at least two New York calendar dates, a latest observation no more than 90 minutes old, and an exact match between the ledger tail and the current normalized snapshot. The separate broker-readiness gate still requires that snapshot's source time to be no more than 45 minutes old; ledger validity cannot renew stale broker state. The snapshot digest excludes only the local `normalized_at` processing timestamp; every broker-sourced field, including `generated_at`, remains bound. Exact equality is intentionally fail-closed: if active-session changes prevent a stable capture, normalize and review from a stable, often after-hours, account state rather than weakening the digest comparison. `--dry-run` does not write the normalized snapshot or mutate the ledger.

The `robinhood_account_drawdown_v2` policy starts from the manual-review base ceiling of `1%` per trade and applies the following multiplier:

| Same-account state | Risk multiplier | Maximum manual-review risk |
|---|---:|---:|
| High-water drawdown less than `5%` | `1.00x` | `1.00%` |
| High-water drawdown at least `5%` and less than `8%` | `0.50x` | `0.50%` |
| High-water drawdown at least `8%` and less than `10%` | `0.25x` | `0.25%` |
| High-water drawdown at least `10%` | `0.00x` | New entries blocked |
| Current New York-session loss at least `3%` | `0.00x` | New entries blocked |

High-water drawdown is `latest equity / maximum observed equity - 1`. The New York-session loss compares the latest observation with the last observation from an earlier New York date, falling back to the first observation on the current New York date when no earlier date exists. A missing, stale, malformed, tampered, mismatched, mixed-account, under-sampled, under-18-hour, or single-New-York-date ledger sets the multiplier to zero. An unexplained adjacent absolute equity change of at least `25%` also blocks for a deliberate operator rebaseline because the system cannot distinguish trading P&L from a deposit, withdrawal, or transfer safely; no automatic rebaseline path is used.

The interlock can never raise requested risk. Its account, policy, snapshot digest, ledger digest, high water, current equity, session reference, loss arithmetic, multiplier, and resulting maximum-risk fraction are bound into the review constraints and revalidated before rendering. It is a new-entry circuit breaker, not continuous monitoring, a liquidation rule, a broker-side stop, or a profit guarantee.

## Robinhood Review and Execution Boundary

The resulting Robinhood packet is review-only and entry-only. It contains no broker credentials or selected account number, expires 10 minutes after creation, and does not place the planning stop or target. Packet v2 binds the semantic packet and rendered prompt to separate SHA-256 digests and revalidates schema, safety controls, calculations, gate context, confirmation summary, content, prompt, and expiry before rendering. A digest detects modification but is not a signature, authentication, or standalone broker authority; a downloaded copy is inspection-only. The local gate requires the planned asset's Edge Lab row to be `live_capital_eligible`, healthy lifecycle validation, fresh broker and research snapshots, a matching allowed account-drawdown interlock, no duplicate or cross-asset same-underlying exposure, no logical working order, and one same active account that satisfies portfolio equity, drawdown-adjusted per-trade risk, total-open allocation, permission, and conservative buying-power checks. It never lets evidence from one asset authorize another or combines equity, loss history, buying power, permissions, or exposure across accounts. The broker snapshot is read once into an immutable request-local capture so account readiness, exposure, duplicates, drawdown attestation, and the recorded snapshot digest cannot come from different reads.

Options and shares both require exact candidate attestation. An option must occur exactly once in the fresh cycle's `manual_review_candidates` and exactly once in the fresh queue's `orders`; both canonical rows must be identical. The attestation freezes the full cycle and queue SHA-256 digests, the canonical row digest, and its first 24 hexadecimal characters as the candidate fingerprint. It also binds the execution profile, evidence lane, and profile-policy version. DTE is recomputed as `expiry date - cycle UTC date`: normal swing requires at least 90 days, while explicit `leaps_swing` requires 365-900 days plus an execution-ready LEAPS assessment with no hard or data blocker. The cycle and queue must retain every no-execution control. Any duplicate, mismatch, stale artifact, altered digest, profile/evidence mismatch, invalid DTE, or execution-enabled control blocks.

A share binds to one fresh actionable `top_shares_*.parquet` row, including symbol, long direction, entry/stop/target geometry, suggested-dollar cap, actionability and research-guard state, artifact time, price-reference provenance, artifact digest, and row fingerprint. Ordinary scans use the last history-bar close and its session as the deterministic geometry reference; this is not a live quote. The attestation therefore permits the explicit state `candidate_quote_available=false`, while still validating any source bid/ask that is actually supplied. In every case the connected Robinhood preflight must obtain a fresh live bid/ask and pass the numeric quote gate before review. A free-form share plan may be sized locally, but it cannot create a copyable broker packet or borrow another candidate's evidence.

The direct preview client or manual fallback task repeats the per-trade and total-open math against the chosen account's freshly read `total_value`, positions, and orders. For options, full debit is both the proposed capital-at-risk amount and the maximum-loss reference. For shares, planned stop loss must fit the risk fraction while full notional is the proposed capital-at-risk amount. Order cost must also fit the smaller of reported buying power and unleveraged buying power. A planner equity assumption may be lower than live equity, but it may not materially overstate it.

The live quote gate is numeric: bid and ask must be positive, ask must be at least bid, quote timestamps must be no more than 120 seconds old, and `(ask - bid) / ((ask + bid) / 2)` must be no greater than the packet cap. These are hard maximums: `1%` for shares, `15%` for normal swing options, and `10%` for `leaps_swing`; configuration cannot raise them, while a candidate may be stricter. Review stops when any field or timestamp is missing or when the live ask exceeds the packet limit. Placement cannot raise the reviewed limit and must revalidate the same candidate and account first.

An option additionally enumerates every complete chain whose `expiration_dates` contains the planned expiry, queries every such `chain_id` through all instrument pages, and requires exactly one active buy-to-open tradable instrument across the total result set. `instrument.chain_symbol` must equal the planned nonnumeric underlying. The linked chain must have `id == instrument.chain_id`, the same symbol, `can_open_position=true`, multiplier `100`, `cash_component=null`, and the exact planned equity in `underlying_instruments`. Missing pages, incomplete cursor linkage, or ambiguous instrument/chain proof blocks, as does any metadata or preview that identifies—or cannot rule out—a nonstandard deliverable. This detection is conservative because adjusted-contract metadata can vary by broker surface.

The packet describes one logical entry idea at a time. It derives one Robinhood idempotency `ref_id` as UUIDv5 of the deterministic packet ID under namespace `60d21b6d-517b-5d2d-b303-6ce65ff6a725`. The identifier preserves one audit identity; it is not placement authority. Manual placement requires a separate 60-second in-memory confirmation and a final state re-read. The optional local controller can schedule bounded account analysis and candidate checks, but it cannot batch broker orders or retry an attempted candidate automatically.

Every direct snapshot collection read follows `data.next` through a bounded, exact cursor chain and requires explicit completion before replacing local state. Every preview preflight must independently refresh the chosen account, positions, orders, exact instrument/chain, and quotes; a saved snapshot is readiness evidence, not a substitute for same-session broker state. Missing pages, stale marks, recent unmatched fills, ambiguity, or packet expiry blocks preview. A placement uses the broker-supported order tool only after a new state review and an explicit manual confirmation or a currently armed guarded-automation capability.

For supported long calls and puts, that same account must report options level 2 or 3. Level 2 supports the narrow permission needed for long calls and puts, but permission is only one prerequisite. It cannot bypass the model firewall, Edge Lab, account drawdown, portfolio risk, full-debit sizing, exact contract identity, quote freshness, spread, tradability, deliverable checks, same-underlying exposure, buying power, preview alerts, state re-read, or explicit confirmation.

The planner stop and target are not resting broker orders. The optional controller runs the normal Optedge pipeline before a decision and can submit one sell-to-close limit order only for one unambiguous long-option holding with an exact broker/lifecycle identity match, a fresh executable quote, no working order or pending transition, and a normal `hard_stop`, `hard_target`, or fully evidenced `close_early` decision. Dynamic exits reuse the existing fused-thesis and learned exit-pressure engine and require a fresh exact ranked-options signal. Multiple holdings, equity positions, adjusted or unresolved contracts, cancellations, exercise, assignment, and generic expiry management remain manual Robinhood actions.

## Guardrails

`risk/research_guard.py` warns or blocks trust when:

- Fewer than 500 closed signals are available.
- Max drawdown is worse than -20%.
- Spread buckets validate poorly.
- An option recommendation has a spread above 15%.
- A new long-option recommendation has negative modeled buyer edge after the round-trip spread.
- Win rate is below a simple breakeven threshold.
- Model weights appear stale.
- Key data engines return no data.

## Exit Reviews

Every scan reanalyzes exits for open options, shares, and futures.

Hard exits always run first and cannot be overridden:

- Options: stop, target, expiry.
- Shares: stop and target.
- Futures: direction-aware stop and target.

Dynamic exit review runs second. It produces an `exit_pressure` score from 0 to 100 and logs every review to `data/exit_reviews.jsonl`.

- 0-39: hold.
- 40-59: watch.
- 60-79: tighten stop.
- 80-100: close early.

The pressure model considers confidence drops, score deterioration, news/sentiment flips, macro regime changes, research guard warnings, engine health, age, repeated reprice failures, and asset-specific risks such as option DTE decay, share trend deterioration, or futures score reversal.

New positions receive a one-hour grace period from soft `tighten_stop` and `close_early` actions so the entry and exit passes in the same scan cannot manufacture zero-duration trades. Hard stops, hard targets, expiry, and research-guard blocks remain immediate. Closed option contracts also have a 24-hour reentry cooldown to prevent same-contract churn.

## Learned Exit Policy

`backtest/exit_learning.py` can refit conservative exit thresholds from closed trades and exit-review history. Learning activates per asset only after at least 100 independent eligible closures, 20 exit reviews, and 10 distinct closed-entry and review days. Same-scan dynamic exits are retained in performance results but excluded from policy learning because they are lifecycle churn rather than swing outcomes.

Learned thresholds are clamped and can move by at most 5 points per refit. Learned policy never overrides hard stops, hard targets, expiry exits, or research-guard blocks. If the policy is missing, malformed, stale, or under-sampled, defaults are used.

Entry readiness is evaluated from executable swing outcomes after slippage when that validation view is available. All closures are still reported, but Watch/Skip rows, zero-size recommendations, blocked entries, and same-scan lifecycle churn cannot make the guard appear statistically mature.

## Adaptive Model Promotion

Source-controlled `config.py` weights are the default priors. A research challenger needs at least 500 independent outcomes across 10 distinct days before it can even be fit as a shadow. An executable champion must meet the stricter promotion evidence: at least 500 out-of-sample predictions, 30 entry days, 30 effective horizon blocks, and three purged expanding-window folds. Repeated forward-test snapshots and same-scan dynamic exits are not treated as independent evidence.

Adaptive research fitting uses after-slippage outcomes and day-balanced sample weights, but its output is an untrusted shadow by default. A normal scan never fits, persists, or immediately consumes a challenger. The active stock-return predictor stays at zero unless a share-only champion carries complete purged expanding-window out-of-sample evidence, fresh policy-bound source digests, and an intact content digest. Option and futures returns cannot train that stock predictor.

`config_runtime.py` is parsed as data rather than executed. Legacy files and research shadows are ignored. A runtime override must use the explicit trusted-champion schema, cover every configured factor, remain normalized and unconcentrated, prove separate share and direct broker-observed option out-of-sample improvement—including positive champion-delta lower bounds and positive doubled-cost means—bind its source policy and outcome digests, and remain no more than 14 days old. The historical snapshot IC report is diagnostic only: it compares current factors with already-realized returns and is not accepted as promotion evidence.

## Human Review

The output should be treated as a prioritized research board. Trade Desk shows at most three freshness-gated exact setups beside **No Trade / hold cash** and ranks them by common safety dimensions rather than incomparable raw asset scores. Fill quality, news shocks, data gaps, spreads, and regime changes can dominate model expectations, and no displayed candidate is an instruction or profit promise.

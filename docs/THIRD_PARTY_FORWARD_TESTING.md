# Third-Party Forward Testing

Optedge has two separate tracking layers:

1. Internal forward testing.
2. External paper execution candidates.

They are intentionally different.

## Internal Forward Testing

Internal forward testing reprices many logged recommendations so the research system can learn from a broad sample. This is useful for factor research, calibration, ranking diagnostics, and model pruning.

Large internal signal counts are normal. They should not be treated as a clean broker-ready trade list.

## External Paper Track

The external paper track is a smaller, cleaner export intended for manual entry into a paper broker or a trading journal.

Generate it with:

```bash
python scripts/export_external_paper_track.py
```

Outputs:

- `data/external_paper_orders.csv`
- `data/external_paper_orders.json`

Preview the selected and excluded rows without writing files:

```bash
python scripts/export_external_paper_track.py --dry-run
```

The exporter reads the latest:

- `data/top_options_*.parquet`
- `data/top_shares_*.parquet`
- `data/top_futures_*.parquet`
- current open-position JSON files
- validation and research-guard summaries when present

It filters down to a small executable subset. By default it removes watch/skip rows, zero-size options/futures, shares without calculable quantity, stale rows, blocked guardrail rows, wide-spread options, malformed contracts, and duplicate names already open in the same direction.

Options in the external paper export default to a minimum of `90` DTE, so the paper track avoids weeklies and very short-dated contracts unless you explicitly override it:

```bash
python scripts/export_external_paper_track.py --min-option-dte 90
```

Default run caps:

- `5` new candidates per run
- `3` options per run
- `2` shares per run
- `2` futures per run
- `30` maximum open external candidates

These caps exist so the external journal stays readable and executable.

## Robinhood Agentic Options Queue

For a small options-focused experiment, Optedge can also create a Robinhood Agentic Trading handoff queue:

```bash
python scripts/export_robinhood_agentic_queue.py --account-budget 500
```

Outputs:

- `data/robinhood_agentic_queue.json`
- `data/robinhood_agentic_prompt.md`
- `data/robinhood_agentic_cycle.json`
- `data/robinhood_agentic_cycle_prompt.md`

This queue is options-only and loss-capped. The local script does not connect to Robinhood, store credentials, or place trades. It prepares a strict candidate file that a Codex/Robinhood MCP session can double-check with live broker/account context before any approval-gated order workflow.

Current Robinhood MCP capabilities can include:

- Account, portfolio, buying-power, equity-position, option-position, equity-order, and option-order reads.
- Real-time equity quotes, option quotes, option chains, option instruments, index lookup, equity fundamentals, and tradability checks.
- Saved scanners/screeners, live scanner runs, scanner creation/editing, and scanner sort updates.
- Watchlist reads plus approval-gated watchlist and option-watchlist writes.
- Equity and single-leg option order review, placement, and cancellation when the selected account is `agentic_allowed=true` and has the required approvals.

The local queue remains a research handoff. A broker-side order is only real after Robinhood confirms it.

Default safety caps:

- up to `5` 90d+ option candidates
- `2` option orders maximum for the agent to submit
- `$500` account budget assumption
- max total premium: `min(50% of budget, $250)`
- max premium per order: `min(30% of budget, $150)`
- minimum DTE: `90`
- BUY_TO_OPEN limit DAY orders only
- no market orders
- no shares, futures, crypto, or margin
- bullish calls are blocked when local SEC filing monitor data shows active S-1/S-3/424B-style offering or dilution risk for that symbol

Preview without writing files:

```bash
python scripts/export_robinhood_agentic_queue.py --account-budget 500 --dry-run
```

Refresh the free/provider option-chain shortlist before building the default 90d+ queue:

```bash
python scripts/export_robinhood_agentic_queue.py --account-budget 500 --refresh-chain --chain-preset swing
```

With `--dry-run`, the chain refresh is previewed but not written or applied to the queue artifacts.
The refresh uses the queue's per-order premium cap, not the full account balance, so a `$500` account defaults to scanning for contracts near the `$150` single-order cap.
Contracts blocked only by premium cap or spread appear as review-only near misses; they are not submit-ready orders.
If you intentionally want to review larger single-contract premiums, set the cap explicitly, for example `--max-premium-per-order 250`.
The queue diagnostics also include a review-only budget ladder showing which larger cap would be the next one to unlock a rejected contract.

For a stricter 6-month-plus version:

```bash
python scripts/export_robinhood_agentic_queue.py --account-budget 500 --min-dte 180 --refresh-chain --chain-preset leaps
```

To refresh the queue automatically after every Optedge scan:

```bash
python run.py --aggressive --bankroll 500 --loop 30 --turbo --no-open --robinhood-agentic-queue --robinhood-budget 500 --robinhood-min-dte 90 --robinhood-max-candidates 5 --robinhood-max-orders 2
```

To refresh the option-chain shortlist inside the normal scan loop:

```bash
python run.py --aggressive --bankroll 500 --loop 30 --turbo --no-open --robinhood-agentic-queue --robinhood-budget 500 --robinhood-min-dte 90 --robinhood-refresh-chain --robinhood-chain-preset swing --robinhood-max-candidates 5 --robinhood-max-orders 2
```

The generated prompt tells the agent to verify exact contract, buying power, current bid/ask/mid, spread, duplicate exposure, active SEC offering/dilution risk, and current news before submitting. If any check is unclear, the agent should skip the order and report why.

## Robinhood Agentic Recurring Cycle

The practical Robinhood Agentic setup is a recurring review cycle, not a one-time file export.

Recommended cadence:

```text
Every 30 minutes while the experiment is active:
1. Let Optedge finish the latest scan.
2. Read data/robinhood_agentic_cycle.json and data/robinhood_agentic_cycle_prompt.md.
3. Verify Robinhood buying power, option approval, exact contract, bid/ask/mid, spread, and current news.
4. Submit at most the queue's max_orders_to_submit as BUY_TO_OPEN limit DAY orders.
5. Check existing Robinhood option positions against Optedge open positions and latest exit reviews.
6. Submit SELL_TO_CLOSE limit DAY orders only when a hard stop, hard target, expiry risk, or close_early review is confirmed.
7. Log every submitted, skipped, held, and closed decision.
```

The cycle packet is the compact handoff for recurring checks. It includes the entry queue, current validation snapshot, open option-position risk summary, recent actionable exit reviews, hard-pause reasons, and explicit agent actions.

Optedge also exposes a local decision journal path in the cycle packet:

- `data/robinhood_agentic_decisions.jsonl`

Use it to append one row for each reviewed entry or exit with a decision such as `submitted`, `skipped`, `held`, `closed`, `updated_stop`, or `reviewed`. This local journal is useful for auditability and debugging the review loop, but it is not broker confirmation and is not third-party verification by itself.

Default mode should be `approval_required`. Do not allow automatic submission until the Robinhood MCP connection, account scope, options approval, buying power, data quality, and Optedge validation are all behaving correctly. If the kill-switch file exists at `data/agentic_trading_disabled.flag`, the agent should skip all entries.

### Codex App Heartbeat

A local Codex heartbeat can review this packet every 30 minutes in the current thread. The heartbeat should:

- Read `data/robinhood_agentic_cycle.json`, `data/robinhood_agentic_cycle_prompt.md`, `data/robinhood_agentic_queue.json`, `data/validation_summary.json`, open-position files, and recent `data/exit_reviews.jsonl`.
- Report whether the entry gate is open or blocked.
- Summarize fresh candidates, open-position exit reviews, and hard-pause triggers.
- Keep order actions approval-gated.
- Use Robinhood MCP tools only for read/check actions unless the user explicitly approves the exact order action in the thread.

Pair the heartbeat with the normal Optedge scan loop:

```bash
python run.py --aggressive --bankroll 500 --loop 30 --turbo --no-open --robinhood-agentic-queue --robinhood-budget 500 --robinhood-min-dte 90 --robinhood-refresh-chain --robinhood-chain-preset swing --robinhood-max-candidates 5 --robinhood-max-orders 2
```

The heartbeat does not replace Optedge scans. It reviews the latest files Optedge generated and prepares the next human-approved action.

### Read-Only Broker Snapshot Reconciliation

The cockpit can compare local Optedge/paper positions against a read-only Robinhood Agentic/MCP snapshot. This keeps local lifecycle state honest without adding broker credentials or automatic order placement to the codebase.

Save a raw JSON bundle of read-only MCP results to:

- `data/robinhood_mcp_snapshot_raw.json`

Useful raw sections are:

- `accounts`
- `portfolio`
- `equity_positions`
- `option_positions`
- `equity_orders`
- `option_orders`

Normalize the raw bundle into the cockpit's expected snapshot shape:

```bash
python scripts/normalize_robinhood_broker_snapshot.py
```

Outputs:

- `data/robinhood_broker_snapshot.json`

Preview without writing:

```bash
python scripts/normalize_robinhood_broker_snapshot.py --dry-run
```

Then open the local cockpit or call:

```bash
python -c "from scripts.local_cockpit import build_broker_reconciliation; import json; print(json.dumps(build_broker_reconciliation(), indent=2))"
```

The reconciliation is still local and read-only. It can show matched positions, broker-only positions, local-only positions, stale snapshots, and whether an agentic account appears option-ready, but it does not submit, cancel, or replace broker orders.

### Local Auto Paper Autopilot

Optedge can automatically take the latest Robinhood Agentic queue in a local paper book:

```bash
python scripts/auto_agentic_paper.py
```

Outputs:

- `data/agentic_paper_positions.json`
- `data/agentic_paper_orders.jsonl`
- `data/robinhood_live_order_tickets.json`

By default, the script only opens local paper positions when the cycle entry gate is open. If validation blocks fresh live entries, it writes no paper order unless you intentionally allow paper-only overrides:

```bash
python scripts/auto_agentic_paper.py --allow-blocked-paper
```

This override is local paper only. It records `paper_override_validation_gate: true` and still writes live tickets as confirmation-required tickets.

Preview without writing:

```bash
python scripts/auto_agentic_paper.py --dry-run
```

The script never submits real broker orders. The live ticket file is a structured checklist for a Robinhood/Codex session after live quote, spread, buying power, duplicate exposure, and news checks. If `data/agentic_trading_disabled.flag` exists, it blocks local paper entries too.

## Asset Separation

Track each asset class separately in the journal:

- Options: contracts, option side, strike, expiry, entry, stop, target, and quantity.
- Shares: ticker, share quantity, entry, stop, target, and suggested dollars.
- Futures: symbol, contract, direction, point-value sizing context, entry, stop, target, and quantity.

Options, shares, and futures have different risk, fill, slippage, and lifecycle behavior. Combining them into one undifferentiated performance number can hide important problems.

## Public Verification

Optedge does not make trades externally verified by creating these files. Public legitimacy comes only after the filtered candidates are entered into a paper or live broker account and that account is connected to a third-party journal or verification service.

This project does not add broker credentials or direct broker execution inside the local codebase. The export is a clean handoff file for paper tracking or a separate approval-gated Codex/Robinhood MCP review session.

## Recommended Workflow

1. Run Optedge normally.
2. Generate the external paper candidate export.
3. Review the CSV manually.
4. Enter only the candidates you choose into a paper broker or journal.
5. Keep options, shares, and futures separated in the journal.
6. Compare third-party results with Optedge's internal validation report.

Internal forward testing is for research breadth. External paper tracking is for clean, reviewable execution evidence.

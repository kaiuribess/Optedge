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

This queue is options-only and loss-capped. It does not connect to Robinhood, store credentials, or place trades. It prepares a strict candidate file that a Codex/Robinhood MCP agent can double-check before submitting any order.

Default safety caps:

- `2` option orders maximum
- `$500` account budget assumption
- max total premium: `min(50% of budget, $250)`
- max premium per order: `min(30% of budget, $150)`
- BUY_TO_OPEN limit DAY orders only
- no market orders
- no shares, futures, crypto, or margin

Preview without writing files:

```bash
python scripts/export_robinhood_agentic_queue.py --account-budget 500 --dry-run
```

The generated prompt tells the agent to verify exact contract, buying power, current bid/ask/mid, spread, duplicate exposure, and current news before submitting. If any check is unclear, the agent should skip the order and report why.

## Asset Separation

Track each asset class separately in the journal:

- Options: contracts, option side, strike, expiry, entry, stop, target, and quantity.
- Shares: ticker, share quantity, entry, stop, target, and suggested dollars.
- Futures: symbol, contract, direction, point-value sizing context, entry, stop, target, and quantity.

Options, shares, and futures have different risk, fill, slippage, and lifecycle behavior. Combining them into one undifferentiated performance number can hide important problems.

## Public Verification

Optedge does not make trades externally verified by creating these files. Public legitimacy comes only after the filtered candidates are entered into a paper or live broker account and that account is connected to a third-party journal or verification service.

This project does not add broker execution, broker credentials, or automated order routing. The export is only a clean handoff file for manual paper tracking.

## Recommended Workflow

1. Run Optedge normally.
2. Generate the external paper candidate export.
3. Review the CSV manually.
4. Enter only the candidates you choose into a paper broker or journal.
5. Keep options, shares, and futures separated in the journal.
6. Compare third-party results with Optedge's internal validation report.

Internal forward testing is for research breadth. External paper tracking is for clean, reviewable execution evidence.

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

## Research-Only Robinhood Option Shortlist

Optedge can create an options-focused research queue:

```bash
python scripts/export_robinhood_agentic_queue.py --account-budget 500
```

Outputs:

- `data/robinhood_agentic_queue.json`
- `data/robinhood_agentic_prompt.md`
- `data/robinhood_agentic_cycle.json`
- `data/robinhood_agentic_cycle_prompt.md`

This queue is options-only and loss-capped, but it is not an execution packet. Manual-review candidates are limited to equity/ETF underlyings and must carry `underlying_type=equity`; index roots and missing or non-equity types are rejected. The exporter sets `execution_enabled=false` and `max_orders_to_submit=0`. Its cycle file keeps `entry_candidates` empty and exposes only `manual_review_candidates` when the validation entry gate is open. Generated queue prompts explicitly prohibit Robinhood review and placement tools. They may compare candidates, record paper decisions, or tell the user why a row was skipped.

Optedge writes a bounded read sequence into `robinhood_agentic_cycle.json` as `robinhood_mcp_read_plan`. It can guide research, live-data checks, and reconciliation, but it grants no authority to use broker write tools. If the user chooses a candidate, rebuild that exact contract in **Trade Desk** to create a separate, fresh manual review packet.

Interactive ticker/contract searches use the narrower `robinhood_research_bridge.py` queue. It requests market research only and deliberately excludes accounts, positions, orders, and credentials. The lookup UI can use exact broker liquidity and quote-age checks as vetoes, but the cache is not an execution channel and never proves a fill.

Creating or refreshing the queue does not start a Codex task, call Robinhood, schedule a review, or place an order.

Default safety caps:

- up to `5` 90d+ option candidates
- up to `2` candidates for manual comparison by default
- `$500` account budget assumption
- max total premium: `min(50% of budget, $250)`
- max premium per order: `min(30% of budget, $150)`
- minimum DTE: `90`
- long BUY_TO_OPEN candidates only
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
Contracts blocked only by premium cap or spread appear as research-only near misses; they are not broker-ready orders.
If you intentionally want to review larger single-contract premiums, set the cap explicitly, for example `--max-premium-per-order 250`.
The queue diagnostics also include a review-only budget ladder showing which larger cap would be the next one to unlock a rejected contract.

For a stricter 6-month-plus version:

```bash
python scripts/export_robinhood_agentic_queue.py --account-budget 500 --min-dte 180 --refresh-chain --chain-preset leaps
```

`--max-orders` is retained as a compatibility name for the manual-comparison cap. It does not increase `max_orders_to_submit`, which remains zero. An Optedge research loop may refresh these local files, but it does not create a recurring Codex message or initiate any Robinhood action.

## One-Packet Robinhood Manual Review

The only live-order handoff is a user-initiated packet built in **Trade Desk**. Each packet represents one logical entry order, expires 10 minutes after creation, contains no broker credentials or selected account number, and says `automation_allowed=false` and `repeat_orders_allowed=false`.

Start the local cockpit:

```bash
python run.py --cockpit
```

The local gate fails closed unless all applicable checks pass:

- The local kill switch is absent, validation evidence is cleared for fresh entries, and data health has no blocking failure.
- A normalized broker snapshot has a real source timestamp and is no more than 45 minutes old. The time at which a stale raw file was normalized does not make it fresh.
- One same active account must provide an explicit positive portfolio `total_value`, agentic access, both explicit `buying_power` and `unleveraged_buying_power`, and options approval when applicable. The gate uses the smaller buying-power figure. V2 readiness does not substitute `equity`, `equity_value`, cash, or another alias for missing portfolio fields.
- The planner equity assumption may be conservative, but it may not exceed that account's live value by more than the greater of `$1` or `1%`. The gate recomputes risk and allocation caps from the same account instead of mixing capacity across accounts.
- Existing Robinhood positions, working orders, and local lifecycle state do not show duplicate exposure. For an option entry, any same-symbol broker position or working open order in the same long-call/long-put direction blocks the plan even when strike or expiry differs.
- For options, the queue and cycle are also no more than 45 minutes old, the entry gate is open, and the exact contract is in `manual_review_candidates`. The candidate must retain a source quote timestamp no more than 45 minutes old plus positive bid/ask values; a freshly reserialized queue cannot make an old quote fresh. Planner price and quantity cannot exceed that candidate's caps.
- For options, the contract is at least 90 DTE, uses a standard `100x` multiplier, and explicitly identifies an equity/ETF underlying with `underlying_type=equity`. Known index roots and symbols beginning with `^` are blocked.

A green local gate still does not authorize placement. Paste the unexpired packet into one connected Robinhood task. That task must refresh the chosen account's portfolio, positions, working orders, exact instrument, tradability, and quote. For options, the live instrument's `underlying_type` must exactly match the packet's expected `equity` type. It recomputes the packet's risk/allocation formulas against that same account, rejects a bid or ask that is missing/zero, rejects a quote older than 120 seconds, and rejects a computed bid/ask spread above the packet cap (`15%` maximum for options and `1%` for shares). It must stop if the live ask is above the packet limit; it may never raise the limit. Only then may it call the broker review tool, present the complete preview, disclosure, alerts, fees, collateral, and estimated cost, and ask the user to confirm the exact reviewed order. After that exact confirmation it may call the matching placement tool once with unchanged fields.

If the placement result is uncertain, query current broker orders before doing anything else. Never create a second logical order as a retry. Submission is not a fill; use Robinhood's returned order state.

### Supported Entry Orders

The current packet deliberately supports a narrow surface:

- Long share or ETF buys.
- Single-leg long calls and puts on equity/ETF underlyings, BUY_TO_OPEN only.
- Limit, good-for-day orders during regular market hours.
- Standard `100x` option contracts only.

It blocks index options (including `^` symbols and known roots such as SPX, NDX, RUT, and VIX), missing/non-equity underlying types, short-share execution, short options, spreads, adjusted option deliverables, market orders, futures, crypto, batches, and unattended execution. It also blocks option permission split across accounts: one and the same account must be `agentic_allowed=true`, funded, and approved at `option_level_2` or `option_level_3`. Equity review needs a funded agentic-accessible account. The connected task must let the user choose or clearly identify the account; it may not silently default one.

See Robinhood's official [Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/), [trading-with-your-agent workflow](https://robinhood.com/us/en/support/articles/trading-with-your-agent/), and [options-level guide](https://robinhood.com/us/en/support/articles/360001227566/) for the broker-side requirements and disclosures.

The packet places the entry only. Stop and target values are planning references, not broker orders. Long-option maximum loss is the full debit, which must fit inside both the risk budget and the allocation cap. Long-share planning shows both stop-based loss and full entry notional. No stop is a guaranteed fill.

Optedge also exposes a local decision journal path in the cycle packet:

- `data/robinhood_agentic_decisions.jsonl`

Use it to record reviewed, skipped, held, or paper decisions. This journal is useful for auditability, but it is not broker confirmation and is not third-party verification by itself.

If `data/agentic_trading_disabled.flag` exists, Trade Desk blocks packet review and the legacy paper helper removes any stale live-ticket artifact.

The optional Optedge scanner loop remains separate. It may refresh research artifacts, but it does not initiate Robinhood review or order actions.

### Read-Only Broker Snapshot Reconciliation

The cockpit can compare local Optedge/paper positions against a read-only Robinhood Agentic/MCP snapshot. This keeps local lifecycle state honest without adding broker credentials or automatic order placement to the codebase.

Robinhood's MCP capability surface may include real order tools, but Optedge treats tool support and account readiness as separate checks:

- `agentic_allowed=true` means the Robinhood connection may expose order tools for that account; it does not itself authorize an order.
- `option_level_2` or `option_level_3` is required for single-leg option orders.
- V2 readiness requires positive `portfolio.total_value` plus explicit `buying_power` and `unleveraged_buying_power`; positive cash or an equity alias does not fill a missing field.
- A split setup, where one account is agentic-accessible and another account is options-approved, is still blocked for agentic option orders.
- The cockpit's **Robinhood MCP capability map** shows read support, write-tool support, current account status, and Optedge's local policy. Broker order actions remain confirmation-required.

Save a raw JSON bundle of read-only MCP results to:

- `data/robinhood_mcp_snapshot_raw.json`

Use the v2 read-bundle shape below for current Robinhood MCP responses. Save the decoded JSON result from each tool, including its `data` object; do not save screenshots or prose summaries.

```json
{
  "schema": "optedge_robinhood_mcp_read_bundle_v2",
  "generated_at": "2026-07-12T19:30:00+00:00",
  "get_accounts": {"data": {"accounts": []}},
  "account_snapshots": [
    {
      "account_number": "FULL_VALUE_USED_FOR_THE_TOOL_CALL",
      "get_portfolio": {"data": {}},
      "get_equity_positions": {"data": {"positions": [], "next": null}},
      "get_option_positions": {"data": {"positions": [], "next": null}},
      "get_equity_orders": {"data": {"orders": [], "next": null}},
      "get_option_orders": {"data": {"orders": [], "next": null}},
      "get_option_instruments": {"data": {"instruments": [], "next": null}}
    }
  ]
}
```

Capture rules:

- Call `get_accounts` first and preserve an exact decoded `data.accounts` list whose every element is an object. Create one `account_snapshots` wrapper for every account it returns, and copy the exact `account_number` used as the request argument into that wrapper. Every wrapper must include all five scoped reads: `get_portfolio` with a decoded `data` object; `get_equity_positions` and `get_option_positions` with `data.positions` lists; and `get_equity_orders` and `get_option_orders` with `data.orders` lists. Every collection element must be an object. Guide/prose-only results, wrong collection names, scalar/null collection elements, or missing decoded envelopes are blocking.
- Never place `get_portfolio`, account positions, or account orders at the top level of a v2 bundle. The wrapper is what proves same-account scope, so a top-level account read is blocking even when every wrapper is present. Only global `get_option_instruments` may remain at the top level.
- `get_option_positions` identifies a holding by `option_id` but does not provide the contract strike or call/put right. Collect every nonzero position's unique `option_id`, call `get_option_instruments` with those IDs, and retain the complete response so the normalizer can join exact contract identity. Missing instrument metadata is blocking.
- Every paginated positions, orders, or instruments page must retain an explicit `data.next` key. A one-page response must end with `data.next: null`; omission is not proof of completion. For multiple pages, store the decoded responses as an ordered list: each intermediate page must have a non-null `data.next`, and the final page must explicitly have `data.next: null`. When a `data.next` URL exposes a cursor, every follow-up page must include `request: {"cursor": "..."}` with that exact cursor so the normalizer can prove linkage. A missing/non-null final cursor, a page after an early null, missing/mismatched cursor linkage, or pages combined from different accounts blocks reconciliation and Trade Desk review.
- Set `generated_at` to the actual UTC time when the read capture finished. The normalizer writes a separate `normalized_at` value for auditability but never uses it to manufacture freshness.
- Use read tools only for this capture. Do not call a review, placement, cancellation, exercise, or other broker-write tool.

The raw file contains full account identifiers because downstream reads require them. It is ignored by Git; keep it under `data/`, do not paste it into issues or logs, and never commit it. The normalized output replaces each full identifier with a stable pseudonymous `account_key`, retains only a masked account label and safe numeric readiness fields, and never emits the original account number. Only an `optedge_robinhood_broker_snapshot_v1` output whose `raw_bundle_schema` is `optedge_robinhood_mcp_read_bundle_v2` can support manual review; legacy/flexible snapshots remain visible for diagnosis but are execution-ineligible.

Normalize the raw bundle into the cockpit's expected snapshot shape:

```bash
python scripts/normalize_robinhood_broker_snapshot.py
```

You can also use the local cockpit's **Normalize raw broker snapshot** button in the Broker / local reconciliation panel.

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

The reconciliation is still local and read-only. It compares signed position type and aggregate quantity per stable account key and exact option contract; contract-key existence alone is never treated as a match. This keeps two same-nickname accounts distinct and surfaces quantity or long/short differences. A nonterminal option order must contain exactly one object leg to establish exact identity. Multi-leg, missing-leg, or malformed-leg orders remain visible but are unresolved and block manual review, so a later spread leg cannot disappear from duplicate-exposure checks. Reconciliation can show matched positions, broker-only positions, local-only positions, stale snapshots, account readiness, and nonterminal orders, but it does not submit, cancel, or replace broker orders. Keep both raw and normalized snapshots under the ignored `data/` directory; never commit private broker data.

### Local Paper Tracker (Explicit Command)

Optedge can copy current queue candidates into its local paper book when you explicitly run:

```bash
python scripts/auto_agentic_paper.py
```

Outputs:

- `data/agentic_paper_positions.json`
- `data/agentic_paper_orders.jsonl`

By default, the script only opens local paper positions when the cycle entry gate is open. If validation blocks fresh entries, it writes no paper order unless you intentionally allow the paper-only override:

```bash
python scripts/auto_agentic_paper.py --allow-blocked-paper
```

This override is local paper only and records `paper_override_validation_gate: true`. It never creates broker authority.

Preview without writing:

```bash
python scripts/auto_agentic_paper.py --dry-run
```

The script never calls broker review or placement tools and never submits a real order. Under the hardened policy it produces zero live tickets and removes a stale `data/robinhood_live_order_tickets.json` file. Use Trade Desk for a new one-order packet. If `data/agentic_trading_disabled.flag` exists, it blocks local paper entries too.

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

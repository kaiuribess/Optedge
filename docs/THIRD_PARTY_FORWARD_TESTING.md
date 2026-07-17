<!-- Purpose: Document approval-gated Robinhood testing workflows. -->

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

## Robinhood Option Research Shortlist

Optedge can create an options-focused research queue:

```bash
python scripts/export_robinhood_agentic_queue.py --account-budget 500
```

Outputs:

- `data/robinhood_agentic_queue.json`
- `data/robinhood_agentic_prompt.md`
- `data/robinhood_agentic_cycle.json`
- `data/robinhood_agentic_cycle_prompt.md`

This queue is an options-only, premium-filtered research shortlist. It is not an execution packet and cannot authorize a Robinhood tool. The schema name `manual_review_candidates` means only that, while the validation entry gate is open, an exact row may be loaded into Trade Desk for a new calculation; the row itself is not broker-ready. When that gate is closed, the list remains empty. Candidates are limited to equity/ETF underlyings and must carry `underlying_type=equity`; index roots and missing or non-equity types are rejected. The default `swing_execution` profile keeps the canonical 90-DTE minimum. The separate explicit `leaps_swing` profile requires 365-900 DTE, the `option_leaps_swing` evidence lane, and its own liquidity, quote, and management policy; DTE alone never changes profiles. Every queue keeps `execution_enabled=false`, `max_orders_to_submit=0`, `auto_submit_allowed=false`, and `does_not_place_orders=true`. Generated prompts prohibit Robinhood review and placement tools. They may compare candidates, record paper decisions, or explain why a row was skipped.

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

For the explicit profile-isolated LEAPS lane:

```bash
python scripts/export_robinhood_agentic_queue.py --account-budget 500 --execution-profile leaps_swing --refresh-chain --chain-preset leaps
```

LEAPS candidates use 365-900 DTE contracts, preferred 365-730 DTE, but the intended thesis lifecycle is short: reviews after 3, 5, and 10 sessions and a maximum planned hold of 20 sessions. This does not promise a fill or profitable exit. The older broad `180+` DTE research preset remains available as `long_dated`; it is not the LEAPS execution profile.

`--max-orders` is retained as a compatibility name for the manual-comparison cap. It does not increase `max_orders_to_submit`, which remains zero. An Optedge research loop may refresh these local files, but it does not create a recurring Codex message or initiate any Robinhood action.

## One-Packet Robinhood Manual Preview

The user-initiated packet built in **Trade Desk** is an expiring broker-preview request, not placement authority. Each packet represents one logical entry idea, expires 10 minutes after creation, contains no broker credentials or selected account number, and says `automation_allowed=false`, `repeat_orders_allowed=false`, and `standalone_broker_authority=false`. Packet v2 hashes its canonical semantic content and rendered prompt separately, then revalidates schema, calculations, safety flags, gate context, content, and expiry before display. These SHA-256 digests detect modification; they are not signatures, authentication, or broker authorization. A downloaded packet is inspection-only. The deterministic UUIDv5 `ref_id` preserves one audit identity but cannot place an order. The live cockpit adds a separate in-memory confirmation or guarded-automation capability after another account and candidate revalidation.

Start the local cockpit:

```bash
python run.py --cockpit
```

The local gate fails closed unless all applicable checks pass:

- The local kill switch is absent, validation evidence is cleared for fresh entries, and data health has no blocking failure.
- The planned asset's current Edge Lab lane is bound to the exact active predictor, runtime-weight, evidence-policy, methodology, and experiment identities; an untrusted or mismatched adaptive artifact cannot borrow another model's results.
- A normalized broker snapshot has a real source timestamp and is no more than 45 minutes old. The time at which a stale raw file was normalized does not make it fresh.
- The same pseudonymous account has an intact v2 equity ledger with at least two ordered observations spanning at least 18 hours and at least two New York calendar dates, a tail no more than 90 minutes old that exactly matches the normalized snapshot, and no unexplained adjacent equity jump of at least 25%. The `1%` manual-review risk ceiling falls to `0.5%` at a 5% high-water drawdown and `0.25%` at 8%; a 10% high-water drawdown or 3% New York-session loss blocks new entries.
- The selected account must reproduce `optedge_robinhood_account_key_derivation_v1`: trim its exact `get_accounts.account_number`, hash UTF-8 `optedge-robinhood-account-v1|<trimmed account number>` with SHA-256, take the first 16 lowercase hexadecimal characters, and prefix `acct_`. The result must exactly match the eligible portfolio and drawdown key. Outside the required private raw capture, never persist or print the raw number. A `...last4` mask is display-only and can identify more than one account.
- One same active account must provide an explicit positive portfolio `total_value`, agentic access, both explicit `buying_power` and `unleveraged_buying_power`, and options approval when applicable. The gate uses the smaller buying-power figure. V2 readiness does not substitute `equity`, `equity_value`, cash, or another alias for missing portfolio fields.
- The planner equity assumption may be conservative, but it may not exceed that account's live value by more than the greater of `$1` or `1%`. The gate recomputes per-trade risk and total-open capacity from the same account instead of mixing capacity across accounts.
- One immutable request-local read of fresh normalized same-account broker positions produces the readiness, reconciliation, duplicate, and conservative exposure result recorded in the packet. Current broker capital at risk plus the proposal's full share notional or full long-option debit must fit `min(planner equity, live same-account total_value) x allocation fraction`. Research recommendations and local paper rows are excluded because they do not prove live capital.
- Any nonzero same-account nonterminal order blocks the total-open calculation until the order resolves. Duplicate-exposure checks separately reject a same-symbol broker position or working order in the proposed direction; for an option entry, that direction check applies even when strike or expiry differs. Existing same-symbol shares block a new option entry, and existing same-symbol options block a new share entry, until the cross-asset concentration is reviewed outside this entry flow.
- For options, the queue and cycle are also no more than 45 minutes old, the entry gate is open, and the exact contract occurs once in `cycle.manual_review_candidates` and once in `queue.orders`. Their canonical rows must match. The attestation binds full cycle, queue, and row SHA-256 digests plus the first 24 row-digest characters as the fingerprint, as well as execution profile, evidence lane, and policy version. The candidate must retain a source quote timestamp no more than 45 minutes old plus positive bid/ask values; reserialization cannot make an old quote fresh. Normal swing requires at least 90 DTE. `leaps_swing` requires 365-900 DTE, `option_leaps_swing`, an execution-ready assessment, and no hard/data blocker. Planner price and quantity cannot exceed candidate caps, and every no-execution control must remain intact.
- For shares, the exact symbol and long direction must match one fresh actionable `top_shares_*.parquet` row. Ordinary scans preserve the last history-bar close/session and derive deterministic entry, stop, and target geometry from it. That reference is not a live quote, so `candidate_quote_available=false` is valid; if a candidate bid/ask is present it must pass its own provenance, freshness, and spread checks. The attestation binds the geometry, price-reference provenance, suggested-dollar cap, actionability, research-guard status, artifact time, source digest, and row fingerprint. Free-form planner input can calculate size locally but cannot copy a Robinhood packet, and a fresh connected Robinhood bid/ask remains mandatory before review.
- For options, the contract satisfies its selected profile's DTE range, uses a `100x` multiplier, and explicitly identifies an equity/ETF underlying with `underlying_type=equity`. Known index roots and symbols beginning with `^` are blocked. Multiplier `100` is not sufficient on its own: review must resolve active buy-to-open tradability and an exact nonnumeric chain root matching the underlying, and must stop if any metadata or preview identifies—or cannot resolve whether there is—a nonstandard deliverable.

A green local gate still does not authorize placement. The direct client or a separately connected Robinhood task must refresh the chosen account's portfolio, positions, working orders, exact instrument, tradability, and quote, following every `data.next`/cursor page to explicit completion; a failed or unlinked page blocks. A recent matching filled opening order blocks until its position is visible. For an option, review must enumerate every chain containing the planned expiry, query every matching `chain_id`, and find exactly one active buy-to-open tradable standard instrument with matching underlying and chain identity. It recomputes same-account risk, rejects unresolved exposure, requires a quote no more than 120 seconds old, and applies hard spread ceilings of `15%` for normal options, `10%` for LEAPS, and `1%` for shares. It must stop if the live ask is above the packet limit; it may never raise that limit. Only then may the broker review tool present its full preview, disclosures, alerts, fees, collateral, and estimated cost.

Manual mode stops at that preview until the user types the exact confirmation in the live cockpit; the 60-second capability is single-use and state is re-read before one limit-order call. Approval-required automation stops with a list of eligible choices. Guarded automatic mode is a separate, explicitly armed, eight-hour local session that can submit one reviewed entry or a narrowly qualified long-option exit inside its execution window. It creates no Codex automation, exposes no generic order dispatcher, and never retries an attempted broker order automatically.

### Supported Preview Requests

The current packet deliberately supports a narrow surface:

- Long share or ETF buys.
- Single-leg long calls and puts on equity/ETF underlyings, BUY_TO_OPEN only.
- Limit, good-for-day orders during regular market hours.
- Conservatively verified standard `100x` option contracts only; multiplier alone is not proof of a standard deliverable.

It blocks index options (including `^` symbols and known roots such as SPX, NDX, RUT, and VIX), missing/non-equity underlying types, numeric or mismatched chain roots, inactive/non-buy-to-open contracts, short-share execution, short options, spreads, adjusted or unresolved option deliverables, market orders, futures, crypto, and batches. Unattended execution is available only through the separate guarded controller, never through a packet or generic broker tool. Adjusted-contract detection is deliberately conservative because the available metadata can differ across live instrument and preview surfaces. It also blocks option permission split across accounts: one and the same account must be `agentic_allowed=true`, funded, and approved at `option_level_2` or `option_level_3`. If options approval was just enabled, run a new direct **Sync broker snapshot once** action, or capture and normalize a new complete v2 bundle through the manual fallback, so the readiness view and equity ledger see the current permission and capital state. Level 2 can provide permission for the supported long-call/long-put flow, but approval cannot bypass evidence, drawdown, full-debit sizing, spread, overlap, deliverable, state-recheck, or confirmation gates. Equity review needs a funded agentic-accessible account. The direct preview or fallback task must let the user choose or clearly identify the account; it may not silently default one.

See Robinhood's official [Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/), [trading-with-your-agent workflow](https://robinhood.com/us/en/support/articles/trading-with-your-agent/), and [options-level guide](https://robinhood.com/us/en/support/articles/360001227566/) for the broker-side requirements and disclosures.

The packet describes an entry preview only. Stop and target values are planning references, not broker orders. Long-option maximum loss is the full debit, while a long share contributes its full entry notional to portfolio exposure. The general planner hard caps per-trade risk at 2% and total-open allocation at 25%; manual Robinhood review uses the stricter drawdown-adjusted 1% base ceiling. The proposed amount must fit those rules and the remaining total-open same-account cap. No stop is a guaranteed fill.

Optedge also exposes a local decision journal path in the cycle packet:

- `data/robinhood_agentic_decisions.jsonl`

Use it to record reviewed, skipped, held, or paper decisions. This journal is useful for auditability, but it is not broker confirmation and is not third-party verification by itself.

If `data/agentic_trading_disabled.flag` exists, Trade Desk blocks packet review and the legacy paper helper removes any stale live-ticket artifact.

The optional Optedge scanner loop remains separate. It may refresh research artifacts, but it does not initiate Robinhood review or order actions.

### Read-Only Broker Snapshot Reconciliation

The preferred path is explicit direct sync: start the cockpit, choose **Connect Robinhood**, complete the official browser OAuth flow, then choose **Sync broker snapshot once**. The action calls only `get_accounts`, each account's portfolio/position/order reads, and any required option-instrument lookup. It follows at most 50 exact cursor-linked pages per collection, aborts before replacement on any incomplete scope, keeps full account identifiers only in memory, and atomically persists only the redacted snapshot and pseudonymous equity ledger. It performs no review or placement call and has no poller, schedule, or retry loop.

The cockpit compares only explicitly broker-linked Optedge lifecycle rows against that point-in-time snapshot. Ordinary research recommendations and local Agentic paper positions remain separate and do not create a matched, broker-only, or local-only live reconciliation result. The direct OAuth grant stays in the operating-system credential vault; passwords, MFA codes, cookies, and raw tokens are never accepted by Optedge.

Robinhood's MCP capability surface may include real order tools, but Optedge treats tool support and account readiness as separate checks:

- `agentic_allowed=true` means Robinhood may expose order tools for that account; Optedge still requires its narrow local placement policy, and the flag alone does not authorize an order.
- `option_level_2` or `option_level_3` is required for single-leg option orders.
- V2 readiness requires positive `portfolio.total_value` plus explicit `buying_power` and `unleveraged_buying_power`; positive cash or an equity alias does not fill a missing field.
- A split setup, where one account is agentic-accessible and another account is options-approved, is still blocked for agentic option orders.
- The cockpit's **Robinhood MCP capability map** separates broker-advertised tool support, current account status, and Optedge's stricter local policy. The only local write path is the fixed confirmed long-option boundary; arbitrary write tools remain unavailable.

If direct sync is unavailable, the manual fallback is to save a raw JSON bundle of read-only MCP results to:

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

The raw file contains full account identifiers because downstream reads require them. It is ignored by Git; keep it under `data/`, do not paste it into issues or logs, and never commit it. The normalized output replaces each full identifier with `acct_` plus the first 16 lowercase hexadecimal characters of SHA-256 over UTF-8 `optedge-robinhood-account-v1|<trimmed account number>`, retains only a `...last4` display mask and safe numeric readiness fields, and never emits the original account number. The mask is not unique and is never an account join key. The normalizer blocks invalid numeric fields instead of coercing them to zero or omitting them, rejects contradictory nonempty quantity aliases, and rejects duplicate or blank account identities; this prevents malformed input from erasing exposure. Only an `optedge_robinhood_broker_snapshot_v1` output whose `raw_bundle_schema` is `optedge_robinhood_mcp_read_bundle_v2` can support manual review; legacy/flexible snapshots remain visible for diagnosis but are execution-ineligible.

Normalize the raw bundle into the cockpit's expected snapshot shape:

```bash
python scripts/normalize_robinhood_broker_snapshot.py
```

You can also use the local cockpit's **Normalize raw broker snapshot** button for this manual fallback. Direct **Sync broker snapshot once** never writes the raw bundle.

Outputs for the repository's real `data/` directory:

- `data/robinhood_broker_snapshot.json`
- `OPTEDGE_STATE_DIR/account_<digest>.json` when that override is set, otherwise `%LOCALAPPDATA%\Optedge\risk\account_<digest>.json` on Windows or `$XDG_STATE_HOME/optedge/risk/account_<digest>.json` (with the standard home fallback) on Unix-like systems
- `account_<digest>.json.bak` beside an established ledger; after a successful append it is sealed to the same newest chain as the primary

When a caller supplies an explicit custom or test data directory, its ledgers remain self-contained under `<data_dir>/robinhood_account_equity_ledgers/` instead of using OS state.

Preview without writing:

```bash
python scripts/normalize_robinhood_broker_snapshot.py --dry-run
```

The dry run writes neither output and never changes an equity ledger. A real normalization seals or deduplicates one observation per eligible account after the normalized snapshot is written. The first observation creates the high-water baseline but remains ineligible for manual review. Readiness needs at least two real observations that span 18 hours and two New York calendar dates; never manufacture a timestamp or edit the ledger by hand. Exact ledger-tail equality with the normalized snapshot is intentional. If account values or broker fields are moving during the session, capture, normalize, and review from a stable—often after-hours—state instead of weakening the match.

Each ledger-file replacement is atomic. During append, the normalizer first protects the prior chain, replaces the primary, then advances the `.bak` sidecar to the same newest chain. A missing primary or required sidecar, a sidecar that is not a valid prefix, or a sidecar that lags the primary blocks review as possible deletion, rollback, divergence, or interrupted final replacement. Rerunning explicit normalization may reseal a validated lagging sidecar to the already-valid primary without adding an observation. Optedge does not automatically establish a new baseline; preserve the state directory and handle any deliberate rebaseline as an explicit operator decision.

Then open the local cockpit or call:

```bash
python -c "from scripts.local_cockpit import build_broker_reconciliation; import json; print(json.dumps(build_broker_reconciliation(), indent=2))"
```

The reconciliation is still local and read-only. It compares signed position type and aggregate quantity per stable account key and exact option contract; contract-key existence alone is never treated as a match. This keeps two same-nickname accounts distinct and surfaces quantity or long/short differences. Share market value must reconcile with quantity times current price when both are present, and malformed quantities, pending transitions, or account identities become blockers rather than zero exposure. A nonterminal option order must contain exactly one object leg to establish exact identity. Multi-leg, missing-leg, or malformed-leg orders remain visible but unresolved and block manual review, so a later spread leg cannot disappear from duplicate-exposure checks. Reconciliation can show matched, broker-only, and local-only results for the explicitly broker-linked subset, plus separate research/paper counts, stale snapshots, account readiness, and nonterminal orders. It does not submit, cancel, or replace broker orders. Keep raw/normalized snapshots and the external pseudonymous equity-ledger state private; neither belongs in Git.

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

The project includes direct official MCP OAuth/read/review support plus a fixed confirmed long-option order boundary. OAuth material stays in the OS credential vault and is not a project credential file. Exports remain handoff files for paper tracking or a separate approval-gated review; they are not broker authority.

## Recommended Workflow

1. Run Optedge normally.
2. Generate the external paper candidate export.
3. Review the CSV manually.
4. Enter only the candidates you choose into a paper broker or journal.
5. Keep options, shares, and futures separated in the journal.
6. Compare third-party results with Optedge's internal validation report.

Internal forward testing is for research breadth. External paper tracking is for clean, reviewable execution evidence.

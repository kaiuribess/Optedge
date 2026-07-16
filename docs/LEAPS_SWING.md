<!-- Purpose: Define the profile-isolated LEAPS swing workflow and its limits. -->

# LEAPS Swing Profile

Optedge treats a LEAPS contract as an instrument with long expiration runway,
not as a promise to hold for a year. The `leaps_swing` profile looks for
single-leg long equity calls or puts with at least 365 days and no more than
900 days remaining, while reviewing the trade thesis after 3, 5, and 10 market
sessions and imposing a 20-session maximum planned hold.

This profile is research and decision support. It cannot guarantee a gain,
prevent a full-premium loss, or make an option liquid. A Robinhood permission
level only establishes that an account may support a strategy; it does not
prove that a contract or trade is suitable.

## Contract Gate

A candidate must satisfy every hard requirement before it can be called
`execution_ready`:

| Requirement | Canonical policy |
|---|---:|
| Days to expiration | `365-900` |
| Preferred expiration runway | `365-730` days |
| Absolute delta | `0.55-0.80` |
| Preferred absolute delta | `0.60-0.75` |
| Bid/ask spread | No more than `10%`; `8%` or less preferred |
| Open interest | At least `250`; `500` preferred |
| Daily volume | At least `10` when open interest is below `500` |
| Optedge confidence | At least `65` |
| Directional edge after costs | Strictly positive |
| Broker quote age | No more than `120` seconds for review |
| Account fit | One contract's full debit must fit the selected risk budget |

Missing delta, liquidity, edge, quote provenance, quote age, or budget data
does not become zero and does not receive a guessed value. It produces a
visible `research_only` state with an execution score of zero. A policy
violation produces `blocked`. Free, delayed, indicative, and modeled quotes
can rank research candidates but cannot authorize broker review.

The profile must be selected explicitly. Optedge never infers LEAPS intent
from DTE alone, because a long-dated contract can belong to a different
strategy with different evidence and exit assumptions.

## Holding and Management References

The default thesis review is 10 sessions, with checkpoints at 3, 5, and 10
sessions and a maximum planned hold of 20 sessions. These are thesis-review
times, not automatic sell instructions.

Until a dedicated exit policy earns sufficient independent evidence, Optedge
uses conservative manual references:

- Review a `25%` premium decline as the loss reference.
- Review a `35%` premium gain as the target reference.
- After a `20%` premium gain, review whether the position should be protected
  near breakeven.

Optedge does not claim these values are optimal. They do not create resting
broker orders, cannot guarantee fills, and cannot cap a gap loss. Exercise,
assignment, corporate-action adjustments, expiration, and sell-to-close
decisions remain separate broker-side responsibilities.

## Independent Evidence Lane

LEAPS uses the `option_leaps_swing` evidence lane. Ordinary option outcomes
cannot authorize it, even when the symbol, direction, or DTE overlaps.
Every signal identity includes the explicit execution profile.

Live-capital eligibility requires every 5-, 10-, and 20-session horizon to
pass independently with:

- 100% exact broker-market-observed option outcomes;
- no pending or excluded rows;
- at least 200 independent outcomes, 30 entry days, and 30 effective
  horizon-length blocks;
- positive average return after recorded costs;
- a positive 90% moving-block lower confidence bound;
- profit factor of at least 1.15, unless the sample contains no losses;
- positive excess return versus SPY;
- positive return after doubling recorded costs; and
- positive results in both the first and recent halves of the sample.

All provenance, cost, spread, benchmark, and timestamp coverage checks must be
complete. Modeled constant-IV option marks remain visible research but never
count toward this live-capital gate. Changing the profile or evidence policy
changes the strategy version, so results produced under an older policy cannot
silently authorize the new one.

## Robinhood Boundary

The profile can prepare one long-call or long-put entry for the same explicit,
active, funded, options-approved account used by every risk check. It does not
support short options, spreads, index options, adjusted contracts, batches, or
unattended placement.

Before a broker preview, Optedge still requires the exact fresh candidate,
profile-specific evidence, a complete account snapshot, intact drawdown
history, conservative buying power, no conflicting exposure or working order,
standard-contract proof, and a current live quote. The order limit cannot rise
after the packet is built. A broker preview is not an order. If the user later
submits through a Robinhood-supported surface, an accepted order is still not
a fill.

The direct Robinhood connection uses the official Trading MCP endpoint and
browser OAuth. Optedge never asks for a Robinhood password, MFA code, cookie,
or API key. OAuth material is stored only in the operating-system credential
vault. Connect and disconnect are explicit user actions, and each read or
preview call is one-shot; there is no scheduler, polling trade loop, or
automatic retry.

## Primary References

- [OIC LEAPS overview](https://www.optionseducation.org/optionsoverview/leaps-overview)
- [OCC Characteristics and Risks of Standardized Options](https://www.theocc.com/getContentAsset/a151a9ae-d784-4a15-bdeb-23a029f50b70/dfc3d011-8f63-43f6-9ed8-4b444333a1d0/riskstoc.pdf)
- [Robinhood Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
- [Robinhood trading-with-your-agent workflow](https://robinhood.com/us/en/support/articles/trading-with-your-agent/)

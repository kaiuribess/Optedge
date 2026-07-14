<!-- Purpose: Explain private local runtime data and its Git boundary. -->

# Private Runtime Data

This directory holds private, machine-generated Optedge state. The guide and placeholders are tracked; live contents stay local.

- Caches downloaded market research.
- Stores reports, positions, outcomes, and learned runtime weights.
- Holds bounded Robinhood research queues and read-only broker snapshots.

The durable real-account equity ledger intentionally does **not** default to this checkout. Set `OPTEDGE_STATE_DIR` to choose its directory; otherwise Optedge uses `%LOCALAPPDATA%\Optedge\risk` on Windows or `$XDG_STATE_HOME/optedge/risk` (with the standard home fallback) on Unix-like systems. Explicit custom and test data directories stay self-contained under their own `robinhood_account_equity_ledgers/` folder. Successful atomic replacements leave each established primary and `.bak` sidecar on the same newest chain. A missing required file, rollback, divergence, or lagging sidecar blocks review instead of silently creating a new baseline; explicit normalization can reseal only a validated lagging sidecar left by an interrupted final write. Optedge does not automatically rebaseline.

Never commit live contents from this directory or the external state directory. Raw broker captures may contain full account identifiers, and pseudonymous equity history is still financially sensitive. Versioned fallback weights belong in `optedge/default_weights/`.

See the [complete project map](../docs/PROJECT_MAP.md) for the purpose of each tracked placeholder and guide.

<!-- Purpose: Explain the installable application and orchestration package. -->

# Optedge Application Package

This is the installable application package and central control layer.

- Routes commands to scans, loop mode, lookup, backtests, forward tests, and the local cockpit.
- Coordinates research engines, risk controls, ranking, tracking, and output generation.
- Holds strategy profiles, engine groups, package versioning, and immutable fallback weights.
- Provides the bounded Robinhood OAuth/read/review client, OS-vault grant storage, one-shot redacted account sync, and a fixed confirmed long-option order boundary.

The package coordinates research and the direct broker connection. Option placement remains fail-closed, single-order, limit-only, and unavailable without explicit confirmation or temporary guarded arming. Learned runtime weights stay in ignored local state.

<!-- Purpose: Explain the installable application and orchestration package. -->

# Optedge Application Package

This is the installable application package and central control layer.

- Routes commands to scans, loop mode, lookup, backtests, forward tests, and the local cockpit.
- Coordinates research engines, risk controls, ranking, tracking, and output generation.
- Holds strategy profiles, engine groups, package versioning, and immutable fallback weights.
- Provides the bounded Robinhood OAuth/read/preview client, OS-vault grant storage, and one-shot redacted account-snapshot sync without exposing placement.

The package coordinates research and the direct read/preview connection but does not place Robinhood orders. Learned runtime weights stay in ignored local state.

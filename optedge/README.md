# Optedge Application Package

This is the installable application package and central control layer.

- Routes commands to scans, loop mode, lookup, backtests, forward tests, and the local cockpit.
- Coordinates research engines, risk controls, ranking, tracking, and output generation.
- Holds strategy profiles, engine groups, package versioning, and immutable fallback weights.

The package coordinates research but does not directly place Robinhood orders. Learned runtime weights stay in ignored local state.

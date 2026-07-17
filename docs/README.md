<!-- Purpose: Index architecture, risk, data, and validation documentation. -->

# Documentation

This directory explains how Optedge works, where its evidence comes from, and how to operate it safely.

| Guide | Purpose |
|---|---|
| [Complete Project Map](PROJECT_MAP.md) | One-line purpose and ownership context for every repository path. |
| [Architecture](ARCHITECTURE.md) | System flow, model and broker trust boundaries, exact candidate/contract identity, asset lifecycles, and local state. |
| [Edge Lab](EDGE_LAB.md) | Independent evidence lanes, exact model provenance, cost coverage, thresholds, statuses, and limitations. |
| [Validation](VALIDATION.md) | Lifecycle and fixed-session evidence, adaptive-model promotion, report artifacts, exclusions, and sample rules. |
| [Risk Model](RISK_MODEL.md) | Sizing, account identity/drawdown, hard spread limits, exact option-chain proof, deterministic packet audit identity, and the manual preview boundary. |
| [LEAPS Swing Profile](LEAPS_SWING.md) | Separate `365-900` DTE contract, liquidity, evidence, holding, and risk policy for short-horizon LEAPS swing review. |
| [Data Sources](DATA_SOURCES.md) | Provider hierarchy, freshness, fallback behavior, and reliability. |
| [Factor Library](FACTOR_LIBRARY.md) | Research factors and the information each engine contributes. |
| [Third-Party Forward Testing](THIRD_PARTY_FORWARD_TESTING.md) | Direct Robinhood OAuth/snapshot sync, manual capture fallback, equity-ledger baselines, exact option attestations, guarded review/execution boundaries, and reproducible external verification. |
| [Free Data Roadmap](FREE_DATA_ROADMAP.md) | Planned improvements to the no-subscription research stack. |
| [Limitations](LIMITATIONS.md) | Known statistical, market-data, execution, and operational constraints. |

Repository participation is covered by the root [contribution guide](../CONTRIBUTING.md), [security policy](../SECURITY.md), and [Code of Conduct](../CODE_OF_CONDUCT.md).

These references may name ignored runtime paths, but they must never contain the corresponding private data.

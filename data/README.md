# Private Runtime Data

This directory holds private, machine-generated Optedge state. The guide and placeholders are tracked; live contents stay local.

- Caches downloaded market research.
- Stores reports, positions, outcomes, and learned runtime weights.
- Holds bounded Robinhood research queues and read-only broker snapshots.

Never commit live contents. Raw broker captures may contain full account identifiers. Versioned fallback weights belong in `optedge/default_weights/`.

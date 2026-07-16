<!-- Purpose: Explain supported security reporting and private-data handling. -->

# Security Policy

## Supported Code

Security fixes target the latest `main` branch. Older commits, local modifications, and third-party forks may not receive fixes.

## Report a Vulnerability Privately

Use GitHub's [private vulnerability reporting form](https://github.com/kaiuribess/Optedge/security/advisories/new). If that form is unavailable, contact the repository owner through a private channel listed on the [maintainer's GitHub profile](https://github.com/kaiuribess). Do not publish exploit details or sensitive data in an issue or discussion.

Include only sanitized information:

- The affected version or commit.
- The affected component and security boundary.
- Reproduction steps using synthetic data.
- Expected impact and any known mitigation.

Never send a Robinhood password, MFA code, cookie, token, full account number, holdings export, raw broker bundle, or real order record. If a live brokerage credential or account may be compromised, rotate or revoke access and contact the broker immediately; do not wait for a project response.

The direct Robinhood client uses the fixed official MCP endpoint and an exact loopback OAuth callback. Passwords, MFA codes, cookies, and API keys are never accepted by the application. OAuth tokens and dynamic client-registration material are stored only in the operating-system credential vault; there is no plaintext file or environment-variable fallback. Oversized Windows Credential Manager values are divided into bounded vault entries, SHA-256 verified, and made visible through a generation-bound manifest only after every chunk is present. Public status omits authorization URLs, codes, state, tokens, and raw account numbers. A direct snapshot action is explicit and bounded, keeps scoped account identifiers only in memory, requires complete cursor-linked reads, and atomically persists only recursively redacted state. The separate finalist check performs bounded market-data reads for one digest-bound option and persists only public contract/quote evidence with a 120-second expiry; it receives no account data and performs no review or write. Official terminal pages that omit `next` are accepted only when the live schema forbids cursors or when the instrument result is a small, exact six-filter scope whose rows all match; ambiguous or broader pagination still fails closed. The client exposes no placement method, and the cockpit exposes no placement route.

## Scope

Useful reports include credential exposure, OAuth state/callback confusion, unauthorized network or file access, bypasses of the loopback/origin controls, broker packet tampering, incomplete pagination accepted as complete, account-scope confusion, redaction failures, or a path that could expose a placement capability.

Trading losses, model performance, data-provider outages, and ordinary market-data disagreements are not security vulnerabilities by themselves. They may still be valid bugs when reproducible with sanitized fixtures.

## Disclosure

The maintainer will review reports on a best-effort basis, coordinate a fix when appropriate, and ask reporters to delay public disclosure until users have a reasonable opportunity to update. This policy does not promise a response time, bounty, or eligibility determination.

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

## Scope

Useful reports include credential exposure, unauthorized network or file access, bypasses of the loopback/origin controls, broker packet tampering, account-scope confusion, or a path that could permit a broker action without the documented explicit confirmation.

Trading losses, model performance, data-provider outages, and ordinary market-data disagreements are not security vulnerabilities by themselves. They may still be valid bugs when reproducible with sanitized fixtures.

## Disclosure

The maintainer will review reports on a best-effort basis, coordinate a fix when appropriate, and ask reporters to delay public disclosure until users have a reasonable opportunity to update. This policy does not promise a response time, bounty, or eligibility determination.

# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.x     | Yes       |

## Reporting a Vulnerability

If you discover a security vulnerability in QUORBIT Protocol, **do not open a public issue**.

Please report it via one of the following channels:

- **Email:** security@quorbit.network
- **Subject line:** `[SECURITY] QUORBIT — <brief description>`

### What to include

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept (if applicable)
- Affected module(s): `bus/` or `busai/`
- Your contact information (optional, for credit)

### Response timeline

- Acknowledgement within **48 hours**
- Initial assessment within **7 days**
- Fix or mitigation plan within **30 days** (critical issues prioritized)

## Scope

In-scope:
- Cryptographic identity (Ed25519 signing/verification)
- Nonce replay attacks
- Consensus manipulation (BFT layer)
- Unauthorized agent registration

Out-of-scope:
- Issues in third-party dependencies (report upstream)
- Social engineering attacks

## Credit

Responsible reporters will be acknowledged in the release notes (unless anonymity is requested).

---

Copyright © 2026 S.N. Panchenko [QB-001] / Quorbit Labs

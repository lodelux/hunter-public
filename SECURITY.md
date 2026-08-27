# Security Policy

## Support status

Hunter is a portfolio/reference snapshot, not a supported hosted product. The
repository owner may address issues in the latest `main` snapshot, but no
release line has a guaranteed security-support window.

## Reporting a Vulnerability

**Please do NOT open a public GitHub issue for security vulnerabilities.**

Use GitHub's private vulnerability-reporting flow for this repository. Do not
include credentials, browser data, application dossiers, or other sensitive
evidence in a public issue.

### What to include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## Scope

The following are considered security issues:

- API token leakage or bypass of authentication middleware
- Credential exposure (LLM API keys, browser session cookies, user passwords)
- Unauthorized access to the browser profile or stored data
- Server-Side Request Forgery (SSRF) via job URLs
- Remote code execution through crafted inputs
- Path traversal in resume file handling

The following are NOT security issues (file as regular bugs):

- Rate limiting bypass on localhost-only endpoints
- Denial of service against a locally bound, single-user instance
- UI rendering issues

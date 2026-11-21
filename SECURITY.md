# Security policy

## Supported versions

The latest published beta revision is supported for security fixes. Older revisions may receive fixes at the maintainer's discretion; there is no long-term support branch. Version numbers are declared in `plugin.yaml` and recorded in `CHANGELOG.md`.

## Reporting a vulnerability

Use [GitHub's private vulnerability reporting form](https://github.com/marcodallagatta/hermes-tingle/security/advisories/new) for this repository. Reports are visible only to the repository maintainers. Do not put vulnerabilities, exploit details, credentials or personal data into a public issue, pull request or discussion. If GitHub does not allow you to submit a private report, contact the repository maintainers through an already authenticated private channel and wait for a confirmed route; do not guess an address or contact an unrelated account.

**Response owner:** the repository maintainers, through the private advisory thread. Acknowledgement and fix timing are best-effort for a pre-1.0 beta; no fixed SLA is promised. Coordinated disclosure is expected: do not publish an exploit or a patch that exposes sensitive details before handling is agreed.

## What to include privately

The affected code commit and Hermes commit/version, a synthetic reproduction, expected and actual behavior, and whether an external effect may already have occurred. Remove real message bodies, identity or destination values, receipts, database files and secrets. Do not test by sending real messages, sabotaging production connections or broadening source grants. Agree a secure transfer method before sharing sensitive evidence.

If a send may have started, preserve that uncertainty. Pause affected jobs through the operator's approved process and preserve current data. Do not replay sends or restore an old database over later records. Never publish an exploit or a patch that exposes sensitive details before coordinated handling is agreed.

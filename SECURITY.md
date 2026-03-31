# Security Policy

## Security Posture

DocMason is built for private local documents.
Security and privacy are core product boundaries, not optional extras.
The tracked repository should not become a public store of private corpus data, compiled knowledge bases, or runtime history.

## Reporting A Vulnerability

DocMason does not yet publish a dedicated private security mailbox.
If you discover a security issue:

- do not post exploit details publicly
- do not attach private source documents, compiled knowledge artifacts, or sensitive runtime logs
- open a minimal public issue only if you need to request a private disclosure channel

## What To Include

When possible, provide:

- affected version, commit, or bundle channel
- host environment and platform
- concise reproduction steps using synthetic or redacted data
- impact summary and any obvious mitigation

## What Not To Include

Do not include:

- confidential source documents
- pasted knowledge-base outputs
- runtime artifacts containing private business data
- secrets, tokens, or screenshots with sensitive content

## Scope Notes

- the source repository is intended to run locally
- generated bundles may perform the bounded release-entry network call documented in [docs/policies/release-entry-and-networking.md](docs/policies/release-entry-and-networking.md)
- host agents such as Codex, Claude Code, and GitHub Copilot have their own privacy and retention behavior outside this repository's control

## Responsible Disclosure Expectations

- prefer synthetic or redacted reproductions
- keep discussion private when the issue could expose user data or update integrity risk
- if maintainers ask for additional artifacts, scrub private content first whenever possible

## Current Limitations

- there is no formal response SLA
- there is no bug bounty program
- Windows and non-native host paths may carry different operational risk and should be described honestly in reports

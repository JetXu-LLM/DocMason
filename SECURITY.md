# Security Policy

## Scope

DocMason is designed to operate over private local documents and private local knowledge artifacts. Security and privacy are therefore not side concerns. They are core product boundaries.

## Current Disclosure Process

There is not yet a dedicated public security mailbox published for this project.

If you discover a security issue:

- do not post exploit details publicly
- do not upload confidential source documents
- do not upload compiled private knowledge-base artifacts
- open a minimal public issue only if needed to request a private disclosure channel without revealing sensitive details

## Privacy Boundary

The repository itself is intended to run locally and should not send corpus content to external cloud APIs by default.

Users may still choose to use external AI agents such as Codex, Claude Code, or GitHub Copilot. Those tools may have their own privacy, retention, or telemetry behavior. The repository does not guarantee the privacy model of external agents chosen by the user.

## Safe Reporting Guidelines

When reporting issues, prefer:

- synthetic examples
- redacted examples
- minimal reproduction steps that avoid confidential data

Avoid:

- company documents
- screenshots containing private business information
- pasted private knowledge-base contents

## Hardening Direction

Future phases should strengthen:

- environment validation
- capability gating
- file provenance checks
- quality validation gates
- documentation around local-only operation and user responsibilities

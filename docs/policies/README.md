# Policy Index

This section groups the public policy and boundary documents for DocMason.

## What Policy Docs Cover

Policy pages explain the boundaries that public readers should rely on:

- security reporting expectations
- data and privacy boundaries
- release-bundle networking behavior
- contributor-facing operating boundaries
- tracked public fixture versus live workspace separation

## Current Policy References

- [Release Entry And Networking](release-entry-and-networking.md)
- [Security Policy](../../SECURITY.md)
- [Contributing Guide](../../CONTRIBUTING.md)

## Current Policy Posture

DocMason is local-first and file-only by default.
The tracked repository should not become a hidden telemetry client, a live private data store, or a public mirror of private runtime state.
Generated bundles may perform the narrow release-entry check documented here.
Everything else should be assumed local unless a public document says otherwise.

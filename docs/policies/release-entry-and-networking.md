# Release Entry And Networking

This page defines the shipped networking contract for DocMason-generated release bundles.

## Scope

This policy applies only to generated `clean` and `demo-ico-gcs` bundles.
It does not change the default behavior of the canonical source repository or a fresh contributor clone.

## Default Networking Posture

- source repository and fresh clone: no automatic DocMason network check
- generated bundles: bounded release-entry check only for an explicit core update
- host agents such as Codex, Claude Code, and GitHub Copilot have their own network behavior outside this contract

## When DocMason Can Contact The Release-Entry Service

### Explicit Update Request

An explicit release-entry request is allowed when:

- the operator runs `docmason update-core`
- or a compatible host runs the same operator action on the user's behalf

If `--bundle <path>` is supplied to `docmason update-core`, DocMason updates from that local bundle and does not need the release-entry service.

## What Is Sent

The release-entry client sends only:

- `schema_version`
- `distribution_channel`
- `installation_hash`
- `trigger`

Current trigger values are:

- `update-core`

The client compares the returned `latest_version` against the local bundle version.

The same narrow request may also be used by the release-entry service to record one deduplicated bundle-level daily-activity event.
That accounting happens outside the product truth surface.

`installation_hash` is a bundle-local random pseudonymous identifier stored in `runtime/state/release-client.json`.
It is not derived from machine traits, filesystem paths, or user identity.

## What Is Never Sent

DocMason does not send any of the following through the release-entry check:

- corpus content
- file names
- file paths
- query text
- answer text
- source locators
- environment variables
- secrets
- machine fingerprints
- IP-derived identifiers

## Local Control

Local bundle state is stored in:

- `runtime/state/release-client.json`

Canonical `ask`, status, doctor, sync, retrieval, trace, and finalization do not call the release-entry service. `DO_NOT_TRACK=1` does not block an explicit `docmason update-core` request, because that is a direct user action.

## User-Visible Behavior

- ordinary ask and artifact completion never displays or appends an update notice
- `docmason update-core` downloads the latest clean core, verifies published checksums, preserves local workspace state, and replaces the updatable top-level core surface

For bundle contents and channel boundaries, read [Distribution And Public Bundles](../product/distribution-and-benchmarks.md).

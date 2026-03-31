# Release Entry And Networking

This page defines the shipped networking contract for DocMason-generated release bundles.

## Scope

This policy applies only to generated `clean` and `demo-ico-gcs` bundles.
It does not change the default behavior of the canonical source repository or a fresh contributor clone.

## Default Networking Posture

- source repository and fresh clone: no automatic DocMason network check
- generated bundles: bounded release-entry check only
- host agents such as Codex, Claude Code, and GitHub Copilot have their own network behavior outside this contract

## When DocMason Can Contact The Release-Entry Service

### Automatic Post-Ask Check

An automatic check is allowed only when all of the following are true:

- the workspace is a generated `clean` or `demo-ico-gcs` bundle
- canonical `ask` has already completed
- at least 20 hours have passed since the last automatic check
- automatic checks are still enabled locally
- `DO_NOT_TRACK=1` is not set

### Explicit Update Request

An explicit release-entry request is allowed when:

- the operator runs `docmason update-core`
- or a compatible host runs the same operator action on the user's behalf

If `--bundle <path>` is supplied to `docmason update-core`, DocMason updates from that local bundle and does not need the release-entry service.

## What Is Sent

The release-entry client sends only:

- `schema_version`
- `distribution_channel`
- `source_version`
- `installation_hash`
- `trigger`

Current trigger values are:

- `ask-auto`
- `update-core`

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

To disable automatic checks for the current bundle, set:

```json
{
  "automatic_check_enabled": false
}
```

`DO_NOT_TRACK=1` disables the automatic post-ask check and the bundle-level daily-activity recording that piggybacks on it.
It does not block an explicit `docmason update-core` request, because that is a direct user action.

## User-Visible Behavior

- a final host-visible ask reply may include a short update notice when a newer bundle exists
- the canonical answer file is not rewritten by that notice
- `docmason update-core` downloads the latest clean core, verifies published checksums, preserves local workspace state, and replaces the updatable top-level core surface

For bundle contents and channel boundaries, read [Distribution And Public Bundles](../product/distribution-and-benchmarks.md).

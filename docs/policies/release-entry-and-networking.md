# Release Entry And Networking

This page defines the shipped networking contract for DocMason release bundles.

## When Networking Can Happen

DocMason itself may make one bounded network request only in this case:

- the workspace is a generated `clean` or `demo-ico-gcs` release bundle
- canonical `ask` has completed and is returning a final host-visible reply
- at least 20 hours have passed since the last automatic release-entry check

The source repository and fresh-clone contributor path do not perform this automatic check.

## What Is Sent

The release-entry client sends only:

- `schema_version`
- `distribution_channel`
- `source_version`
- `installation_hash`
- `trigger = ask-auto`

`installation_hash` is a random local pseudonymous identifier created inside the bundle-local
`runtime/state/release-client.json` file.
It is not derived from machine traits, user identity, corpus contents, or filesystem paths.

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

The single local release-entry control file is:

- `runtime/state/release-client.json`

To disable the automatic check for the current bundle, set:

```json
{
  "automatic_check_enabled": false
}
```

`DO_NOT_TRACK=1` also disables the automatic check and bundle-only DAU recording regardless of the
local file setting.

## Reset

To reset the local release-entry state, remove:

- `runtime/state/release-client.json`

The next eligible automatic check will recreate the file with a new random local installation hash.

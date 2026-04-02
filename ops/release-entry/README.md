# Release-Entry Worker

This directory contains the minimal Cloudflare Worker deployment surface for DocMason's bounded
release-entry update checks and bundle-only deduplicated daily-activity accounting.

## Purpose

The worker has two jobs only:

1. accept one bounded bundle client request after canonical ask completion
2. expose the current release metadata for that bundle channel while recording one deduplicated
   daily-activity event for both bounded auto-check and explicit `docmason update-core` calls

It is intentionally not part of the product truth surface.
It does not receive corpus data, file metadata, query text, answer text, source locators,
environment variables, secrets, machine fingerprints, or IP-derived identifiers.

## Endpoints

- `POST /v1/check`
  - bundle client endpoint
  - request fields:
    - `schema_version`
    - `distribution_channel`
    - `installation_hash`
    - `trigger`
- `POST /v1/admin/release-current`
  - release-publish endpoint
  - protected by `Authorization: Bearer <DOCMASON_RELEASE_ENTRY_ADMIN_TOKEN>`

## Storage

- D1 table `release_current`
  - one row per bundle channel
- D1 table `daily_activity`
  - deduplicated by `(event_day, installation_hash, distribution_channel)`
  - counts daily active installations, not user identity

The canonical schema lives in [schema.sql](schema.sql).
The Python-side contract mirror used by tests lives in
`src/docmason/release_entry_service.py`.

## Deployment Notes

1. Create a D1 database and bind it as `DB`.
2. Set the Worker secret `DOCMASON_RELEASE_ENTRY_ADMIN_TOKEN`.
3. Update [wrangler.toml](wrangler.toml) with the real database ID and Worker name.
4. Apply [schema.sql](schema.sql) to the bound D1 database.
5. Deploy the Worker with Wrangler.

The release GitHub Actions workflow publishes release metadata to the admin endpoint after assets are
uploaded.

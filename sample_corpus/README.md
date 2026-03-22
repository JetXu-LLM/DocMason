# Public Sample Corpus

`sample_corpus/` is the tracked public fixture boundary for DocMason.

It exists for three reasons:

- provide a stable public demo corpus for release bundles and first-product evaluation
- support contributor regression testing without redefining the meaning of live `original_doc/`
- keep public sample maintenance auditable through normal commits, review, and release notes

This directory is intentionally not the same thing as live `original_doc/`.

- `sample_corpus/` is tracked public fixture content
- `original_doc/` remains the writable user-managed corpus boundary during ordinary workspace use

When a contributor or demo script needs the sample corpus in a real workspace, it should copy a
curated preset from `sample_corpus/` into `original_doc/` explicitly.

# ICO + GCS Public Sample Corpus

This sample corpus is the canonical public demo preset for DocMason.

Design goals:

- keep the corpus small enough for a fast public demo
- keep the sources high quality and publicly attributable
- cover two adjacent but distinct knowledge domains:
  - `ICO` for governance, compliance, audit, and AI/data-protection risk
  - `GCS` for campaign planning, communication execution, and evaluation
- preserve clean source-level boundaries with first-level `ico/` and `gcs/` directories

This corpus is maintained from official UK public-sector sources.
It contains deterministic Markdown snapshots of anchor pages plus same-site official downloadable
attachments in DocMason-supported formats such as `pdf`, `pptx`, `docx`, and `xlsx`.

License notes:

- upstream public materials are sourced from UK public-sector pages that cite Open Government
  Licence v3.0 or equivalent public re-use guidance on the referenced pages
- each managed file is recorded in `manifest.json` with its upstream URL, checksum, and local path
- maintainers should verify the current public re-use terms before materially expanding the sample

Operational rule:

- do not edit the live `original_doc/` tree in the canonical repo to update this sample
- refresh `sample_corpus/ico-gcs/`, review the resulting diff, then regenerate demo bundles

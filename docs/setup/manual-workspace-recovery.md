# Manual Workspace Bootstrap And Recovery

Use this document only when the normal automation path is unavailable or incomplete:

- `./scripts/bootstrap-workspace.sh --yes`
- `docmason prepare --yes`

This is not the ordinary native Codex/macOS first-run path.
On that path, DocMason should prefer the controlled bootstrap asset launcher plus the governed
machine-baseline install flow before dropping here.

The launcher contract is now strict:

- it performs bounded liveness probing instead of trusting an arbitrary bootstrap interpreter forever
- it prefers repo-local managed Python, then the repo-local bootstrap venv, then an explicit
  `DOCMASON_BOOTSTRAP_PYTHON` override, then the controlled UV bootstrap asset path
- when it has to fetch that controlled bootstrap asset, it should do so through a shared user
  cache and then build the repo-local bootstrap helper from there
- it rejects obviously broken bootstrap candidates such as shebang-only recursive stubs,
  startup-silent candidates, and timed-out launcher chains

The normal product expectation is still:

- repo-local managed Python `3.13` under `.docmason/toolchain/python/`
- repo-local `.venv` anchored to that managed Python
- editable `docmason` install from the current workspace root
- `runtime/bootstrap_state.json` recorded with `isolation_grade = self-contained`
- `doctor --json` as the verification step

This deeper fallback exists for agent environments such as non-native shells, unsupported
platforms, path-moved workspaces, or tools that cannot run the committed launcher directly.

## Target End State

An environment is good enough for ordinary DocMason work when all of these are true:

1. A bootstrap or repair Python 3.11 or newer is available.
2. `.docmason/toolchain/python/current/bin/python3.13` exists.
3. `.venv/bin/python` resolves under `.docmason/toolchain/python/`.
4. `runtime/bootstrap_state.json` reports `schema_version = 4` and `isolation_grade = self-contained`.
5. `docmason doctor --json` reports the environment checks honestly.
6. On native Codex/macOS, the machine baseline is ready: Homebrew plus LibreOffice are present.
7. If the current corpus includes Office files, LibreOffice is installed before sync runs.

## Lowest-Risk Manual Repair Order

1. Confirm the real workspace root.
   - It should contain `pyproject.toml`, `docmason.yaml`, `src/docmason/`, and `scripts/`.
   - If the repository was moved, always work from the new real path.

2. If the normal launcher cannot obtain a bootstrap runtime, decide whether you are still on an
   ordinary path.
   - For native Codex/macOS ordinary cold starts, go back and complete the governed launcher path
     first, including any explicit `Default permissions` versus `Full access` boundary.
   - Only continue here when the launcher itself is unavailable or cannot finish honestly.

3. Find a healthy supported Python.
   - Preferred manual fallback order: an explicit `DOCMASON_BOOTSTRAP_PYTHON`, then a known-good
     `python3.13`, `python3.12`, or `python3.11`.
   - On macOS, Homebrew Python is acceptable for this manual fallback path.
   - On Linux, use the distro package manager or an already-installed supported Python.
   - Do not trust a wrapper that resolves to a recursive `#!/usr/bin/env python3` stub or a
     candidate that hangs on a trivial startup probe.

4. Run the repo-local prepare flow from source with that bootstrap Python.

Preferred path:

```bash
PYTHONPATH=src /absolute/path/to/python3.11 -m docmason prepare --yes
```

If you want machine-readable output:

```bash
PYTHONPATH=src /absolute/path/to/python3.11 -m docmason prepare --yes --json
```

5. If `prepare` cannot provision `uv` automatically, repair the repo-local bootstrap helper venv.

Preferred path:

```bash
/absolute/path/to/python3.11 -m venv .docmason/toolchain/bootstrap/venv
.docmason/toolchain/bootstrap/venv/bin/python -m ensurepip --upgrade
.docmason/toolchain/bootstrap/venv/bin/python -m pip install uv
PYTHONPATH=src .docmason/toolchain/bootstrap/venv/bin/python -m docmason prepare --yes
```

6. Verify.

```bash
.venv/bin/python -m docmason doctor --json
.venv/bin/python -m docmason status --json
.venv/bin/python -c "import pathlib; print(pathlib.Path('.venv/bin/python').resolve())"
```

The resolved `.venv/bin/python` path should sit under `.docmason/toolchain/python/`.

## Workspace Move Repair

If the repository was renamed or moved:

1. Do not trust an old `.venv` or cached bootstrap marker from the previous path.
2. Re-run `docmason prepare --yes` from the new workspace root through a supported bootstrap Python.
3. Confirm that `runtime/bootstrap_state.json` now records the new `workspace_root`.
4. Confirm that `.venv/bin/python` resolves under the new `.docmason/toolchain/python/` tree.

## Office Rendering Requirement

If the current source corpus includes `.pptx`, `.ppt`, `.docx`, `.doc`, `.xlsx`, or `.xls`,
DocMason needs LibreOffice before sync can build the knowledge base correctly.

- native Codex/macOS ordinary path:
  - prefer returning to the governed launcher and `docmason prepare --yes`
  - if the thread is still in Codex `Default permissions`, switch it to `Full access` first

- macOS with Homebrew:

```bash
brew install --cask libreoffice-still
```

- macOS without Homebrew:
  - install LibreOffice from `https://www.libreoffice.org/download/download/`
  - move the app into `/Applications`

- Linux:
  - install LibreOffice with the distro package manager or the official packages
  - ensure `soffice` is on `PATH`

Then rerun:

```bash
.venv/bin/python -m docmason doctor --json
```

## PDF Rendering And Region Extraction Requirement

If the current source corpus includes `.pdf`, DocMason now expects the full repo-local PDF stack:

- `PyMuPDF` for region-level visual extraction
- `pypdfium2` for render generation
- `pypdf` for conservative text and page handling
- `pillow` for image output

Preferred repair path:

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

If you need a narrower manual repair inside an existing `.venv`:

```bash
.venv/bin/python -m pip install --upgrade PyMuPDF pypdfium2 pypdf pillow
```

Then rerun:

```bash
.venv/bin/python -m docmason doctor --json
.venv/bin/python -m docmason status --json
```

## Windows Or Non-Native Agent Environments

Windows is not the primary supported DocMason platform in the current product contract.
Even so, an advanced agent may still perform a best-effort local bootstrap if the user explicitly
wants that path.

Use the same invariants:

- keep the environment repo-local
- let `prepare` build repo-local managed Python `3.13`
- let `prepare` rebuild `.venv` against that managed runtime
- verify with `doctor`

Typical Windows commands:

```powershell
py -3.11 -m venv .docmason\toolchain\bootstrap\venv
.docmason\toolchain\bootstrap\venv\Scripts\python.exe -m ensurepip --upgrade
.docmason\toolchain\bootstrap\venv\Scripts\python.exe -m pip install uv
$env:PYTHONPATH = "src"
.docmason\toolchain\bootstrap\venv\Scripts\python.exe -m docmason prepare --yes
.venv\Scripts\python.exe -m docmason doctor --json
```

If this succeeds, the agent should still describe Windows as a compatibility or best-effort path,
not as the native supported workflow.

## Agent Rule

If scripts or canonical skills are insufficient for the current shell or platform:

- do not stop at “unsupported” if the missing work is only deterministic local setup
- do the minimal safe repair automatically when no extra permissions are needed
- ask the user only when permissions, GUI installers, or policy-sensitive package installs are
  required
- after manual repair, always rerun `doctor --json`

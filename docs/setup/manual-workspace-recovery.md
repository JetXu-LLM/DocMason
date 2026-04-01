# Manual Workspace Bootstrap And Recovery

Use this page only when the normal automation path cannot finish honestly:

- `./scripts/bootstrap-workspace.sh --yes`
- `docmason prepare --yes`

This is a fallback reference, not the ordinary first-run story.

## Default Expectation

On the native macOS path, DocMason should set up a repo-local managed Python, a repo-local `.venv`, and any needed machine-level dependencies with minimal manual work.
If the normal automation path succeeds, stop here and use it.

## Recovery Goal

A workspace is ready for ordinary DocMason work when all of the following are true:

1. a bootstrap Python 3.11 or newer is available
2. `.docmason/toolchain/python/current/bin/python3.13` exists
3. `.venv/bin/python` resolves under `.docmason/toolchain/python/`
4. `runtime/bootstrap_state.json` records a self-contained environment
5. `docmason doctor --json` reports the environment honestly
6. required machine-level tools are present for the current corpus

## Lowest-Risk Recovery Order

### 1. Confirm The Real Workspace Root

The root should contain at least:

- `pyproject.toml`
- `docmason.yaml`
- `src/docmason/`
- `scripts/`

If the repository was moved or renamed, always work from the new real path.

### 2. Choose A Supported Bootstrap Python

Preferred manual fallback order:

- `DOCMASON_BOOTSTRAP_PYTHON`
- a known-good `python3.13`
- `python3.12`
- `python3.11`

On macOS, Homebrew Python is acceptable for this fallback path.
On a non-native or experimental host path, bring your own already installed supported Python.

### 3. Run The Repo-Local Prepare Flow

Preferred command:

```bash
PYTHONPATH=src /absolute/path/to/python3.11 -m docmason prepare --yes
```

Machine-readable variant:

```bash
PYTHONPATH=src /absolute/path/to/python3.11 -m docmason prepare --yes --json
```

### 4. Repair The Bootstrap Helper If `uv` Provisioning Fails

If `prepare` cannot provision `uv` automatically, repair the repo-local bootstrap helper venv:

```bash
/absolute/path/to/python3.11 -m venv .docmason/toolchain/bootstrap/venv
.docmason/toolchain/bootstrap/venv/bin/python -m ensurepip --upgrade
.docmason/toolchain/bootstrap/venv/bin/python -m pip install uv
PYTHONPATH=src .docmason/toolchain/bootstrap/venv/bin/python -m docmason prepare --yes
```

### 5. Verify The Result

```bash
.venv/bin/python -m docmason doctor --json
.venv/bin/python -m docmason status --json
.venv/bin/python -c "import pathlib; print(pathlib.Path('.venv/bin/python').resolve())"
```

The resolved `.venv/bin/python` path should sit under `.docmason/toolchain/python/`.

## Workspace Move Repair

If the repository was renamed or moved:

1. do not trust an old `.venv` from the previous path
2. rerun `docmason prepare --yes` from the new workspace root
3. confirm that `runtime/bootstrap_state.json` now records the new `workspace_root`
4. confirm that `.venv/bin/python` resolves under the new `.docmason/toolchain/python/` tree

## Dependency Checks

### Office Files

If the current source corpus includes `.pptx`, `.ppt`, `.docx`, `.doc`, `.xlsx`, or `.xls`, install LibreOffice before syncing.

- native Codex/macOS ordinary path:
  - prefer going back to the governed launcher and `docmason prepare --yes`
  - if the thread is still in Codex `Default permissions`, switch it to `Full access` first

- macOS with Homebrew:

```bash
brew install --cask libreoffice-still
```

- macOS without Homebrew:
  - install LibreOffice from `https://www.libreoffice.org/download/download/`
  - move the app into `/Applications`

- non-native or experimental host paths:
  - install a compatible LibreOffice build with your operating system package manager or the official packages
  - ensure `soffice` is on `PATH`

Then rerun:

```bash
.venv/bin/python -m docmason doctor --json
```

### PDF Corpora

If the current source corpus includes `.pdf`, keep the full repo-local PDF stack installed:

- `PyMuPDF`
- `pypdfium2`
- `pypdf`
- `pillow`

Preferred repair path:

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

If you need a narrower repair inside an existing `.venv`:

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
Even so, an advanced agent may still perform a best-effort local bootstrap if the user explicitly wants that path.

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

If this succeeds, describe Windows as a compatibility or best-effort path, not as the native supported workflow.

## When To Stop And Ask For Help

Stop and escalate when:

- machine-level dependencies require GUI installation or policy approval
- host permissions are insufficient for the required system changes
- `doctor --json` still reports a degraded or mixed environment after repair
- the workspace cannot reach a self-contained repo-local runtime

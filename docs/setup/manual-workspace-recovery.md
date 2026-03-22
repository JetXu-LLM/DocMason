# Manual Workspace Bootstrap And Recovery

Use this document only when the normal automation path is unavailable or incomplete:

- `./scripts/bootstrap-workspace.sh --yes`
- `docmason prepare --yes`

The normal product expectation is still:

- repo-local `.venv`
- editable `docmason` install from the current workspace root
- `uv` preferred, `venv` plus `pip` supported
- `doctor --json` as the verification step

This deeper fallback exists for agent environments such as non-native shells, unsupported
platforms, path-moved workspaces, or tools that cannot run the committed launcher directly.

## Target End State

An environment is good enough for ordinary DocMason work when all of these are true:

1. Python 3.11 or newer is available.
2. The workspace has a repo-local virtual environment.
3. `docmason` imports from the current workspace `src/` tree, not from another checkout.
4. `docmason doctor --json` reports the environment checks honestly.
5. If the current corpus includes Office files, LibreOffice is installed before sync runs.

## Lowest-Risk Manual Repair Order

1. Confirm the real workspace root.
   - It should contain `pyproject.toml`, `docmason.yaml`, `src/docmason/`, and `scripts/`.
   - If the repository was moved, always work from the new real path.

2. Find a supported Python.
   - Preferred: `python3.11`, `python3.12`, `python3.13`, or newer.
   - On macOS, Homebrew Python is acceptable.
   - On Linux, use the distro package manager or an already-installed supported Python.

3. Create or repair `.venv`.

Preferred path:

```bash
uv venv --allow-existing --python /absolute/path/to/python3.11 .venv
```

Fallback path:

```bash
/absolute/path/to/python3.11 -m venv .venv
```

4. Install DocMason into that `.venv`.

Preferred path:

```bash
uv pip install --python .venv/bin/python -e ".[dev]"
```

Fallback path:

```bash
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
```

5. Finish the repo-local bootstrap.

```bash
.venv/bin/python -m docmason prepare --yes
```

6. Verify.

```bash
.venv/bin/python -m docmason doctor --json
.venv/bin/python -m docmason status --json
```

## Workspace Move Repair

If the repository was renamed or moved:

1. Do not trust an old editable install path.
2. Recreate or repair `.venv` from the new workspace root.
3. Re-run the editable install into that `.venv`.
4. Re-run `docmason prepare --yes` so `runtime/bootstrap_state.json` reflects the new root.

## Office Rendering Requirement

If the current source corpus includes `.pptx`, `.ppt`, `.docx`, `.doc`, `.xlsx`, or `.xls`,
DocMason needs LibreOffice before sync can build the knowledge base correctly.

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

## Windows Or Non-Native Agent Environments

Windows is not the primary supported DocMason platform in the current product contract.
Even so, an advanced agent may still perform a best-effort local bootstrap if the user explicitly
wants that path.

Use the same invariants:

- keep the environment repo-local
- create `.venv`
- install the package in editable mode
- verify with `doctor`

Typical Windows commands:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m docmason prepare --yes
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

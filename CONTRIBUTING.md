# Contributing to clipshelf

Issues and pull requests are welcome.

## Before you start

- Look at the open issues. Claim an issue with a comment before you write code.
- A security problem does not go into a public issue. Report it through
  private vulnerability reporting. See [SECURITY.md](SECURITY.md).
- The development guide is [docs/development.md](docs/development.md). It
  explains the code layout, the tests, and the dependency pinning.

## Setup

```
python3.13 -m venv --clear --upgrade-deps .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
```

`--clear` resets the venv, so a rerun stays clean: the checks below install
ruff and pip-audit into it, and the license gate must not see them.

## Checks

Run these commands from the repository root, in this order. The order matters:
the license gate runs before ruff and pip-audit join the venv, as in CI.

```
.venv/bin/python scripts/lock_deps.py check
.venv/bin/python check_licenses.py
.venv/bin/python -m pip install ruff==0.16.9 pip-audit==2.10.1
.venv/bin/python -m ruff check
.venv/bin/python -m pip_audit -r requirements.lock --no-deps
CLIPSHELF_DATA_DIR=/tmp/clipshelf-check PY=.venv/bin/python sh scripts/test.sh
```

Every command must exit 0. The test command needs a throwaway data dir, never
`./data` (the app writes its secret key there).

A change to `android/` adds the Android checks. See
[docs/development.md](docs/development.md#tests) for those commands.

## Pull requests

1. Keep one logical change in one pull request.
2. Run all checks before you open the pull request. CI runs the same checks.
3. Write the commit subject in lowercase and in the imperative, for example
   `fix: keep mailed invitation links alive across cooldown resends`.
4. Link the issue in the pull request description.

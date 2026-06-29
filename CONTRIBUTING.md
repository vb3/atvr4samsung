# Contributing

Thanks for helping harden `atvr4samsung`.

## Development setup

```sh
git clone <repo-url>
cd atvr4samsung
pipx install .
```

For editable development, use a virtual environment instead:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## Conventions

`AGENTS.md` is the source of truth for code conventions. In short:

- Prefer meaningful tests over superficial coverage.
- Comment why, not what.
- Use type hints.
- Keep pure layers import-light and dependency-free where practical.

## Commit rules

- Bump `version` in `pyproject.toml` every commit. It is the single source of truth;
  `src/atvr4samsung/__init__.py` derives `__version__` from package metadata.
  Bump the patch level for routine commits; bump the **minor** level to cut a release (CI builds + publishes the wheel when the version becomes `X.Y.0`).
- Put code and its tests in the same commit.
- Keep commits small and focused.
- A `Co-authored-by` trailer is fine for AI-assisted commits.

## Security and licensing rules

- Never commit secrets or real device data: PINs, Samsung tokens, real TV IP/MAC
  addresses, or pairing keys. These are gitignored.
- If you add a new secret or state path, add it to `.gitignore` in the same change.
- Honor the LGPL boundary: `samsungtvws` and `zeroconf` are imported unmodified as
  normal dependencies. Update `THIRD_PARTY_NOTICES.md` when dependencies change.

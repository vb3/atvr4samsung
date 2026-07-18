# Contributing

Thanks for helping harden `atvr4samsung`.

## Development setup

```sh
git clone <repo-url>
cd atvr4samsung
uv sync --all-extras --locked
uv run --frozen --no-sync python -m pytest
```

Container contract tests do not require Docker. When Docker is available, build the production image
with `docker build --build-arg VERSION=... --build-arg VCS_REF=... .`.

## Conventions

`AGENTS.md` is the source of truth for code conventions. In short:

- Prefer meaningful tests over superficial coverage.
- Comment why, not what.
- Use type hints.
- Keep pure layers import-light and dependency-free where practical.

## Commit rules

- Bump `version` in `pyproject.toml` every commit. It is the single source of truth;
  `src/atvr4samsung/__init__.py` derives `__version__` from package metadata.
  Bump the patch level for routine commits and use minor/major versions for features or breaking
  changes. Every strictly newer stable version publishes the OCI image, deployment bundle, offline
  bundle attestation, and signed release manifest.
- Keep the `atvr4samsung` GHCR package public in GitHub's package settings. The release workflow
  verifies public visibility and an anonymous digest pull before publishing the immutable release;
  it does not use a long-lived package-administration token. On the first container release, the
  initial workflow run creates the package and intentionally stops at this gate; make that package
  public in GitHub, then rerun the failed workflow.
- Keep GitHub immutable releases enabled for the repository. This is repository administration
  state and is verified outside the workflow so CI does not need a long-lived administration
  credential.
- Put code and its tests in the same commit.
- Keep commits small and focused.
- A `Co-authored-by` trailer is fine for AI-assisted commits.

## Security and licensing rules

- Never commit secrets or real device data: PINs, Samsung tokens, real TV IP/MAC
  addresses, or pairing keys. These are gitignored.
- If you add a new secret or state path, add it to `.gitignore` in the same change.
- Honor the LGPL boundary: `samsungtvws` and `zeroconf` are imported unmodified as
  normal dependencies. Update `THIRD_PARTY_NOTICES.md` when dependencies change.

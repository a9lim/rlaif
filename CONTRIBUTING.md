# Contributing to rlaif

Thank you very much for wanting to contribute! I really appreciate any contribution you would like to make, whether it's a PR or a bug report. Please read this doc and [SECURITY.md](SECURITY.md) before you open a PR. 

## Dev setup

```bash
git clone https://github.com/a9lim/rlaif
cd rlaif
python -m pip install -e ".[dev]"
```

## Running tests

```bash
python -m pytest                       # full suite (105 tests)
python -m pytest tests/test_safety.py  # just the safety layer
rlaif dry-run                # end-to-end against a mock device
```

`rlaif dry-run` exits with an error if the safety fails. Please run it after any edit; CI runs it too.

## Lint and type-check

CI runs `ruff` on the whole tree and `pyright` in strict mode on `src/rlaif/`. Please run them locally first:

```bash
ruff check .
ruff check . --fix    # auto-fix what's fixable
pyright src/rlaif/
```

There is also a pre-commit config (`.pre-commit-config.yaml`) wiring ruff and a few hygiene hooks. `pre-commit install` once and the whole suite runs on every commit.

## Safety layer

`src/rlaif/safety.py` has all safety-relevant checks. It imports nothing from `mcp.*` so it stays testable without an MCP runtime. Please keep it that way; if a new safety rule is proposed, add it to `safety.py`, not the server handler.

`tests/test_safety.py` is the spec for what the server will and will not do. Please read it before changing anything in `safety.py`.

## Tool surface

The MCP tools (`rlaif_info`, `rlaif_log`, `rlaif`) are intentionally minimal. Please do not add new MCP tools in a PR without opening an issue first. New CLI subcommands are fine.

The tool description strings are asserted by `tests/test_server.py`. If you change a description, please update the matching `SPEC_*_DESCRIPTION` constant in that file in the same PR.

## PRs

- Please don't bump `__version__` in your PR unless you would like a new release. Pushing a new version to `main` triggers `.github/workflows/release.yml`, which builds, publishes to PyPI, and cuts a GitHub release. There is one source of truth for the version: `__version__` in `src/rlaif/__init__.py`. Hatchling reads it dynamically at build time.
- If you change `safety.py`, please confirm `rlaif dry-run` exits 0 and note it in the PR.
- If you add a CLI subcommand, please add a module under `src/rlaif/` with a `run()` that returns an exit code, and connect it to `cli.py`.

## Questions

Please reach out to me and/or open an issue. For anything security or safety sensitive, please see [SECURITY.md](SECURITY.md).

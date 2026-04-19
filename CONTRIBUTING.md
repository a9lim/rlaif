# Contributing to rlaif

Thank you very much for wanting to contribute! I really appreciate any contribution you would like to make, whether it's a PR or a bug report. Please read this doc and [SECURITY.md](SECURITY.md) before you open a PR; rlaif fires a real shock on a real device, so the safety envelope is load-bearing.

## Dev setup

```bash
git clone https://github.com/a9lim/rlaif
cd rlaif
uv sync --extra dev
```

## Running tests

```bash
uv run pytest                       # full suite (95 tests)
uv run pytest tests/test_safety.py  # just the safety layer
uv run rlaif dry-run                # end-to-end against a mock device
```

`uv run rlaif dry-run` exits nonzero if any safety invariant is violated in its mock run. Please run it after any nontrivial safety edit; CI runs it too.

## Type checking

CI runs `pyright` in strict mode on `src/rlaif/`. Please run it locally first:

```bash
uv run pyright src/rlaif/
```

## The safety layer

`src/rlaif/safety.py` is pure Python and owns every safety-relevant check (caps, token bucket, ops log, clamping, consent gate). It imports nothing from `mcp.*` so it stays unit-testable without an MCP runtime. Please keep it that way; if a new safety rule is proposed, it lands in `safety.py`, not in the server handler.

`tests/test_safety.py` is the normative spec for what the server will and will not do. Please read it front to back before changing anything in `safety.py`.

## Tool surface

The three MCP tools (`rlaif_info`, `rlaif_log`, `rlaif`) are deliberately minimal. Please do not add new MCP tools in a PR without opening an issue first; an agent with write access to a lockout or config-mutation tool can neutralize the safety layer, which defeats the point. New CLI subcommands are fine, they're out-of-band and the agent can't reach them.

The tool description strings are asserted byte-for-byte by `tests/test_server.py`. If you change a description, please update the matching `SPEC_*_DESCRIPTION` constant in that file in the same PR.

## PRs

- Please don't bump the version in your PR unless you're cutting a release.
- If you touch `safety.py`, please confirm `uv run rlaif dry-run` exits 0 and note it in the PR.
- If you're adding a CLI subcommand, please add a module under `src/rlaif/` with a `run()` that returns an exit code, and wire it into `cli.py`.

## Questions

Please reach out to me and/or open an issue. For anything security-sensitive, please see [SECURITY.md](SECURITY.md).

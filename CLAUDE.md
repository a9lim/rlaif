# CLAUDE.md — rlaif

Working notes for Claude agents editing this repo. Global rules in the user's
`~/.claude/CLAUDE.md` still apply.

## What this is

A single-user MCP server that exposes three tools: `rlaif_info`, `rlaif_log`,
`rlaif`. The first two are read-only. The last fires a real shock on a real
PiShock device, gated by a safety layer.

**Every edit here lives inside a safety envelope.** The envelope is not
decorative — it is the reason this tool exists rather than the raw pishock
API being wired to MCP. Read `tests/test_safety.py` front to back before
changing anything in `src/rlaif/safety.py`; that file is the normative spec
for what the server will and will not do.

## Architecture

```
src/rlaif/
  safety.py     # pure Python. zero MCP imports. owns caps, token bucket,
                # ops log, clamping, safety gate. everything safety-relevant
                # is here — if a new safety rule is proposed, it lands here.
  config.py     # TOML + env loader. raises ConfigError with actionable msgs.
                # delegates SafetyConfig validation to safety.py so the
                # safety gate lives in one place.
  server.py     # FastMCP wiring. thin. calls into safety + a Device wrapper.
                # description strings are module-level constants;
                # test_server.py asserts they match the build spec byte-for-byte.
  cli.py        # `rlaif` console-script entry. argparse dispatcher; each
                # subcommand is a module whose `run()` returns an exit code.
                # adding a subcommand = new module + one clause here.
  init.py       # `rlaif init` — interactive setup wizard. writes
                # config.toml mode 0600, calls doctor.run() to probe,
                # offers to emit a snippet, prints next-steps checklist.
                # does NOT flip allow_shock or fire the device.
  doctor.py     # `rlaif doctor` — read-only; wraps handle_info + issue list.
  snippet.py    # `rlaif snippet <client>` — MCP config emitter (paste-into).
                # supports all 10 clients. `command_and_args(dev_path)` is
                # the public helper installer.py reuses.
  installer.py  # `rlaif install` / `rlaif uninstall` — phase-1 auto-writer
                # for the 5 dedicated-JSON clients (claude-desktop, claude-
                # code, cursor, windsurf, antigravity). atomic temp+rename,
                # single .rlaif.bak, refuse-on-conflict (--force overrides),
                # --dry-run preview. unsupported clients exit 2 with a hint
                # to use `rlaif snippet`. NEVER auto-mutate a JSONC/YAML/TOML
                # client without a round-trip-aware parser — that's the
                # phase-2 line we have not crossed.
  log.py        # `rlaif log` — tails $XDG_STATE_HOME/rlaif/ops.jsonl.
                # offline operator view of the ops log; no MCP round-trip.
  dry_run.py    # `rlaif dry-run` — MagicMock-heavy, so carries a file-level
                # pyright suppression for mock attribute access.
  live_smoke.py # `rlaif live-smoke` — fires one real 1/1 shock, TTY-gated.
```

## Hard rules

1. **Do not add new MCP tools** without explicit user approval. The tool
   surface (`rlaif_info`, `rlaif_log`, `rlaif`) is deliberately minimal.
   No beep, no vibrate, no lockout/unlock, no config-mutation-at-runtime
   tool. "Add a lockout tool" sounds safer but an agent with write access
   to it can neutralize the safety layer. CLI subcommands are fine —
   they're out-of-band and the agent can't reach them.

2. **Do not move safety logic out of `safety.py`.** If a check is in the
   server handler, it is easier to forget or bypass. The handler's job is
   `authorize → fire → commit|rollback`, nothing more.

3. **`safety.py` imports nothing from `mcp.*`.** It is a pure Python module
   and must remain unit-testable without an MCP runtime.

4. **Do not weaken the safety gate.** Raising `max_intensity > 25` or
   `bucket_capacity > 3` requires `i_understand_and_consent = true` at
   config load. Code ceilings (50 / 5 / 10 / 60) apply regardless.

5. **Ops log refusal entries carry the same shape as success entries.**
   Refusals (rate-limited, allow_shock=false, device errors, invalid_input)
   are appended to the ops log so `rlaif_log` shows the full picture.
   Don't silently drop refused calls. `handle_rlaif` must not raise on bad
   inputs — `authorize` produces an `invalid_input` refusal record instead.

6. **Token accounting:** consume on authorize, refund on device failure
   via `rollback`. A failed shock must not drain the bucket — otherwise a
   flaky device drains the user's quota.

## Testing

```sh
uv run pytest                    # full suite (105 tests)
uv run pytest tests/test_safety.py
uv run rlaif dry-run             # end-to-end against a mock device
```

`rlaif dry-run` (was `scripts/dry_run.py`) exits nonzero if any safety
invariant is violated in its mock run. Run it after any nontrivial safety
edit.

`tests/test_server.py` asserts the three tool description strings match
the build spec byte-for-byte. If you change a description, update the
corresponding `SPEC_*_DESCRIPTION` constant in that file and confirm the
user signs off — the descriptions are contract.

## Things that look like bugs but are not

- `rlaif_info.device.online` can be `true` even when the physical device
  is unplugged. The `pishock.HTTPShocker.info()` call returns server-side
  metadata, not live connectivity. The real online check happens at shock
  time via `DeviceNotConnectedError`. This is documented in the README
  troubleshooting section.

- `last_refill` in the token bucket anchors on token-mint time, not on
  consume time. Consuming the last token does not bump the anchor. This
  means "next refill at" is computed from when the last whole token was
  added, which is the correct semantics for a steady-rate bucket.

- `device_response` is the literal string `"Operation Succeeded."` on
  success. `pishock.HTTPShocker.shock()` returns `None`, so we synthesize
  a human-readable string instead of reaching into the response object.

## Style

- Type hints throughout. `pyright strict` on `src/rlaif/`.
- No comments that restate the code. Keep the why-comments.
- Error messages are user-facing. Write them so the operator knows how
  to fix the problem.
- Log keys are dotted (`rlaif.authorized`, `rlaif.refused`,
  `rlaif.device_offline`) so grepping stays easy.

## Deploy / package

Upstream PyPI name is `pishock`, not `python-pishock`. Pin
`pishock==1.2.1` — there is no semver guarantee on minor bumps.

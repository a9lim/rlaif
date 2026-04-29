# CLAUDE.md — rlaif

Working notes for Claude agents editing this repo. Global rules in the user's
`~/.claude/CLAUDE.md` still apply.

## What this is

A single-user MCP server with a pluggable shock backend. The MCP surface
is three tools: `rlaif_info`, `rlaif_log`, `rlaif`. The first two are
read-only. The last fires a real shock on a real device, gated by the
safety layer. Backends supported today: PiShock, OpenShock (cloud or
self-hosted).

**Every edit here lives inside a safety envelope.** The envelope is not
decorative — it is the reason this tool exists rather than the raw
PiShock/OpenShock APIs being wired to MCP. Read `tests/test_safety.py`
front to back before changing anything in `src/rlaif/safety.py`; that
file is the normative spec for what the server will and will not do.

## Architecture

```
src/rlaif/
  safety.py     # pure Python. zero MCP imports, zero provider imports.
                # owns caps, token bucket, ops log, clamping, safety gate,
                # `reason` field. everything safety-relevant is here — if a
                # new safety rule is proposed, it lands here.
  config.py     # TOML + env loader. raises ConfigError with actionable msgs.
                # delegates SafetyConfig validation to safety.py so the
                # safety gate lives in one place. owns the [provider] schema
                # resolution and the legacy [auth] back-compat shim.
  providers/
    base.py     # Provider ABC + normalized error taxonomy
                # (DeviceOfflineError, DevicePausedError, ShockNotAllowedError,
                # ProviderAuthError, ProviderError) + DeviceInfo dataclass.
                # The error classes are the seam server.py catches on; new
                # providers MUST raise these, not their own SDK exceptions.
    pishock.py  # PiShockProvider — wraps the upstream `pishock` package and
                # normalizes its exceptions onto base.py.
    openshock.py# OpenShockProvider — httpx-based; talks to api.openshock.app
                # by default, takes a `base_url` for self-hosted backends.
                # Auth is `OpenShockToken: <api_token>` header. Endpoints:
                # GET /1/shockers/{id} for info, POST /2/shockers/control
                # for the shock. Body shape lives in the docstring.
    mock.py     # MockProvider — in-memory, used by dry_run + tests instead
                # of MagicMock-spec gymnastics.
  server.py     # FastMCP wiring. thin. depends on Provider, never on a
                # specific backend SDK. description constants live as
                # *_DESCRIPTION_FRAME; compose_rlaif_description prepends
                # the operator-authored purpose preamble.
                # `Device` is a back-compat alias around PiShockProvider for
                # tests that injected pre-built pishock mocks; new tests
                # should use MockProvider.
  cli.py        # `rlaif` console-script entry. argparse dispatcher; each
                # subcommand is a module whose `run()` returns an exit code.
                # adding a subcommand = new module + one clause here.
  init.py       # `rlaif init` — interactive setup wizard. asks which
                # provider, prompts for matching credentials, writes
                # config.toml mode 0600, calls doctor.run() to probe,
                # offers to emit a snippet, prints next-steps checklist.
                # does NOT flip allow_shock or fire the device.
  doctor.py     # `rlaif doctor` — read-only; provider-agnostic; uses
                # the configured Provider via build_provider().
  snippet.py    # `rlaif snippet <client>` — MCP config emitter (paste-into).
                # supports all 10 clients. `command_and_args(dev_path)` is
                # the public helper installer.py reuses.
  installer.py  # `rlaif install` / `rlaif uninstall` — auto-writer for
                # 8 clients across 3 formats: JSON (claude-desktop,
                # claude-code, cursor, windsurf, antigravity, opencode),
                # TOML via tomlkit (codex), YAML via ruamel.yaml (hermes).
                # opencode uses its own JSON schema (`mcp.<name>` with
                # `command` as an array) — OpencodeJsonAdapter handles it,
                # and the install path detects JSONC and redirects to
                # snippet. atomic temp+rename, single .rlaif.bak,
                # refuse-on-conflict (--force overrides), --dry-run preview.
                # vscode and zed stay manual: vscode is JSONC and zed
                # shares its settings file with arbitrary editor state.
                # that's the line we still have not crossed.
  log.py        # `rlaif log` — tails $XDG_STATE_HOME/rlaif/ops.jsonl, plus
                # `--stats` for histograms (intensity buckets, refusal
                # reasons, hourly volume, energy proxy intensity*duration_s).
                # offline operator view; no MCP round-trip.
  dry_run.py    # `rlaif dry-run` — uses MockProvider, exits nonzero on
                # any safety invariant violation. clean of MagicMock
                # gymnastics now that providers/mock.py exists.
  live_smoke.py # `rlaif live-smoke` — fires one real 1/1 shock,
                # provider-agnostic, TTY-gated.
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

3. **`safety.py` imports nothing from `mcp.*` and nothing from
   `rlaif.providers.*`.** It is a pure Python module and must remain
   unit-testable without an MCP runtime or a backend SDK.

4. **Do not weaken the safety gate.** Raising `max_intensity > 25` or
   `bucket_capacity > 3` requires `i_understand_and_consent = true` at
   config load. Code ceilings (50 / 5 / 10 / 60) apply regardless of
   provider.

5. **Ops log refusal entries carry the same shape as success entries.**
   Refusals (rate-limited, allow_shock=false, device errors,
   invalid_input, provider auth errors) are appended to the ops log so
   `rlaif_log` shows the full picture. Don't silently drop refused
   calls. `handle_rlaif` must not raise on bad inputs; `authorize`
   produces an `invalid_input` refusal record instead.

6. **Token accounting:** consume on authorize, refund on device failure
   via `rollback`. A failed shock must not drain the bucket — otherwise a
   flaky device drains the user's quota.

7. **New providers MUST raise the normalized error types from
   `providers/base.py`, not their own SDK exceptions.** The server
   catches `DeviceOfflineError`, `DevicePausedError`,
   `ShockNotAllowedError`, `ProviderAuthError`, and `ProviderError`. If a
   provider lets a foreign exception escape, it bypasses the rollback +
   logging path and the safety log gets out of sync with reality.

## Adding a new provider

1. Subclass `Provider` in `src/rlaif/providers/<name>.py`.
2. Implement `info() -> DeviceInfo` (must not raise; on failure return
   `DeviceInfo(online=False, error=...)`).
3. Implement `shock(*, intensity, duration_s) -> str` (raises one of the
   typed errors on failure; returns a short response string on success).
4. Add a `from_config(raw, *, label)` classmethod.
5. Wire the kind name into `build_provider` in `providers/__init__.py`,
   add it to `SUPPORTED_PROVIDER_KINDS` in `config.py`, and teach
   `_build_provider_config` how to validate the new credential fields.
6. Add tests in `tests/test_providers.py` using `httpx.MockTransport`
   (for HTTP backends) or `MagicMock` (for SDK-wrapped backends).
7. Update `init.py` to offer the new kind in the wizard.
8. Document it in the README and `config.example.toml`.

## Reason field

The `rlaif` MCP tool takes an optional `reason: str` parameter that
threads through `safety.authorize`, lands on the `OpRecord`, and surfaces
in `rlaif_log`. It is **never gated on**. The safety layer treats it as
opaque text, clipped to `REASON_MAX_LEN`, with blank-string normalized
to `None`. If you find yourself wanting to make a decision based on the
reason field, that decision belongs in `[tool] purpose` instead, where
the operator (not the agent) controls it.

## Tool description frames

`server.py` defines `RLAIF_*_DESCRIPTION_FRAME` constants. The MCP tools
register with a description that is either the frame verbatim (info, log)
or `compose_rlaif_description(purpose) = "Operator purpose:\n<purpose>\n\n" + RLAIF_DESCRIPTION_FRAME`
when a `[tool] purpose` is configured (rlaif).

`tests/test_server.py` asserts the frames match `SPEC_*_DESCRIPTION`
byte-for-byte. If you change a frame, update the matching `SPEC_*` and
confirm the user signs off; descriptions are contract.

## Testing

```sh
uv run pytest                       # full suite (200+ tests)
uv run pytest tests/test_safety.py  # safety spec
uv run pytest tests/test_providers.py # provider abstractions, including OpenShock HTTP via MockTransport
uv run rlaif dry-run                # end-to-end against a mock provider
```

`rlaif dry-run` exits nonzero if any safety invariant is violated in its
mock run. Run it after any nontrivial safety edit.

## Things that look like bugs but are not

- `rlaif_info.device.online` can be `true` even when the physical device
  is unplugged. For PiShock the `.info()` call returns server-side
  metadata, not live connectivity. For OpenShock the GET `/1/shockers/{id}`
  call only confirms the shocker exists in the backend; the hub may not
  be connected. The real online check happens at shock time via
  `DeviceOfflineError`. This is documented in the README troubleshooting
  section.

- `last_refill` in the token bucket anchors on token-mint time, not on
  consume time. Consuming the last token does not bump the anchor. This
  means "next refill at" is computed from when the last whole token was
  added, which is the correct semantics for a steady-rate bucket.

- `device_response` is the literal string `"Operation Succeeded."` on
  success regardless of provider. The PiShock SDK returns `None` and the
  OpenShock API returns a `LegacyEmptyResponse`; both providers
  synthesize the same human-readable string so the response shape is
  stable across backends.

- The legacy `[auth]` config section is still accepted on load and maps
  to `provider.kind = "pishock"`. If a config has both `[auth]` and
  `[provider]`, rlaif refuses to start; mixing the schemas is an accident
  waiting to happen. New configs should use `[provider.pishock]` or
  `[provider.openshock]`.

## Style

- Type hints throughout. `pyright strict` on `src/rlaif/`.
- No comments that restate the code. Keep the why-comments.
- Error messages are user-facing. Write them so the operator knows how
  to fix the problem.
- Log keys are dotted (`rlaif.authorized`, `rlaif.refused`,
  `rlaif.device_offline`, `rlaif.auth_error`, `rlaif.provider_error`)
  so grepping stays easy.

## Deploy / package

PiShock SDK upstream PyPI name is `pishock`, not `python-pishock`.
Pin `pishock==1.2.1` — there is no semver guarantee on minor bumps.
OpenShock support uses `httpx>=0.27.0` directly, no SDK dependency.

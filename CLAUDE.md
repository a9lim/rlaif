# CLAUDE.md — rlaif

Working notes for Claude agents editing this repo. Global rules in the user's
`~/.claude/CLAUDE.md` still apply.

## What this is

A single-user MCP server with two pluggable reinforcement channels for the human user:

- **negative** (shock): PiShock, OpenShock.
- **positive** (vibration): Intiface Central via the buttplug.io protocol.

The MCP surface is up to four tools: `rlaif_info`, `rlaif_log`, `rlaif_negative`, `rlaif_positive`. The first two are read-only and span both channels. The last two fire real devices, gated by the safety layer. Each fire-tool only registers when its channel is configured; either or both channels can be present.

**Every edit here lives inside a safety envelope.** The envelope is not decorative. It is the reason this tool exists rather than the raw PiShock, OpenShock, or buttplug APIs being wired straight to MCP. Read `tests/test_safety.py` front to back before changing anything in `src/rlaif/safety.py`; that file is the normative spec for what the server will and will not do.

## Architecture

```
src/rlaif/
  safety.py       # pure Python. zero MCP imports, zero provider imports.
                  # owns caps, token bucket, ops log, clamping, safety gate,
                  # `reason` field, channel specs. ChannelSpec encodes per-
                  # channel constants (input ranges, ceilings, consent
                  # thresholds, dotted config_path). NEGATIVE_CHANNEL and
                  # POSITIVE_CHANNEL are the presets. Everything safety-
                  # relevant lands here.
  config.py       # TOML + env loader. raises ConfigError with actionable
                  # messages. owns the [negative]/[positive] schema and the
                  # 1.x-rejection check. ChannelConfig is uniform across
                  # channels (kind, raw, label, safety, purpose); the kind
                  # discriminator picks the matching provider.
  providers/      # negative-channel backends (shock).
    base.py       # Provider ABC + normalized error taxonomy
                  # (DeviceOfflineError, DevicePausedError, ShockNotAllowedError,
                  # ProviderAuthError, ProviderError) + DeviceInfo dataclass.
                  # The error classes are the seam server.py catches on; new
                  # providers MUST raise these, not their own SDK exceptions.
    pishock.py    # PiShockProvider — wraps the upstream `pishock` package and
                  # normalizes its exceptions onto base.py.
    openshock.py  # OpenShockProvider — httpx-based; talks to api.openshock.app
                  # by default, takes a `base_url` for self-hosted backends.
                  # Auth is `OpenShockToken: <api_token>` header. Endpoints:
                  # GET /1/shockers/{id} for info, POST /2/shockers/control
                  # for the shock. Body shape lives in the docstring.
    mock.py       # MockProvider — in-memory, used by dry_run + tests instead
                  # of MagicMock-spec gymnastics.
  rewards/        # positive-channel backends (vibration). Parallel namespace
                  # to providers/ so a code change cannot accidentally cross-
                  # wire negative onto positive or vice versa.
    base.py       # RewardProvider ABC + RewardProviderError taxonomy
                  # (RewardDeviceOfflineError, RewardProviderAuthError,
                  # RewardWatchdogError, RewardProviderError) +
                  # RewardDeviceInfo dataclass. RewardWatchdogError exists
                  # specifically for the disconnect contract; the server
                  # does NOT refund the token on watchdog because the
                  # device may still be running.
    intiface.py   # IntifaceProvider — wraps `buttplug` package over WS.
                  # Persistent asyncio loop in a daemon thread bridges the
                  # async client to our sync tool handlers. Disconnect
                  # watchdog has three layers: explicit stop after duration_s,
                  # atexit handler stop_all_devices, signal handler that
                  # chains to default disposition. Hard kills (SIGKILL) are
                  # documented as residual risk.
    mock.py       # MockRewardProvider — in-memory, used by dry_run + tests.
  server.py       # FastMCP wiring. thin. Depends on Provider and
                  # RewardProvider, never on a specific backend SDK. Four
                  # description constants live as RLAIF_*_DESCRIPTION_FRAME;
                  # `_compose_description(frame, purpose)` prepends the
                  # matching channel's operator-authored purpose. Both fire
                  # handlers route through `_fire_channel`, which takes a
                  # per-channel dispatch table mapping each typed error
                  # class to (log_suffix, refund: bool); the watchdog entry
                  # in the positive table is the lone refund=False row.
                  # NegativeRuntime / PositiveRuntime bundle SafetyState +
                  # provider for each channel, injectable for tests.
                  # build_negative_runtime / build_positive_runtime are the
                  # canonical constructors; doctor.py and live_smoke.py
                  # reuse them. rlaif_info combines per-channel
                  # info_snapshot dicts; rlaif_log interleaves both
                  # channels' ops by timestamp.
  cli.py          # `rlaif` console-script entry. argparse dispatcher; each
                  # subcommand is a module whose `run()` returns an exit code.
                  # adding a subcommand = new module + one clause here.
  init.py         # `rlaif init` — interactive setup wizard. asks which
                  # channels (negative-only / positive-only / both), prompts
                  # for matching credentials per channel, writes config.toml
                  # mode 0600, calls doctor.run() to probe each channel,
                  # multi-selects MCP clients to set up — auto-installs via
                  # installer.install() where supported, falls back to
                  # snippet_run() for vscode/zed — and prints a per-channel
                  # next-steps checklist. does NOT flip allow on either side.
  doctor.py       # `rlaif doctor` — read-only, channel-agnostic. Probes
                  # whichever channels are configured; surfaces per-channel
                  # issues with the dotted TOML path the operator should fix.
  _clients.py     # single registry of every supported MCP client. owns
                  # `ClientRecord` (name, location_hint, snippet_builder,
                  # path_fn | None, format_adapter | None) and
                  # `CLIENTS_REGISTRY: dict[str, ClientRecord]`. snippet.py,
                  # installer.py, init.py, and cli.py all read from here.
                  # `path_fn is None` means manual-only (vscode, zed);
                  # `INSTALL_SUPPORTED` is derived from the registry.
                  # Adding a new client = one entry here.
  snippet.py      # `rlaif snippet <client>` — MCP config emitter (paste-into).
                  # supports all 10 clients. owns the per-client snippet
                  # builders (`claude_desktop_path`, `codex_builder`,
                  # `hermes_builder`, `json_mcp_servers_builder`,
                  # `vscode_builder`, `zed_builder`, opencode helpers) that
                  # `_clients.py` wires into `CLIENTS_REGISTRY`. The
                  # `command_and_args(dev_path)` helper is still the seam
                  # installer.py uses to spell out the bin invocation.
  installer.py    # `rlaif install` / `rlaif uninstall` — auto-writer for
                  # 8 clients across 3 formats: JSON (claude-desktop,
                  # claude-code, cursor, windsurf, antigravity, opencode),
                  # TOML via tomlkit (codex), YAML via ruamel.yaml (hermes).
                  # opencode uses its own JSON schema (`mcp.<name>` with
                  # `command` as an array); OpencodeJsonAdapter handles it,
                  # and the install path detects JSONC and redirects to
                  # snippet. atomic temp+rename, single .rlaif.bak,
                  # refuse-on-conflict (--force overrides), --dry-run preview.
                  # Owns the `FormatAdapter` classes and the per-client path
                  # resolvers; `_clients.py` pulls them in by name.
                  # vscode and zed stay manual: vscode is JSONC and zed
                  # shares its settings file with arbitrary editor state.
                  # That's the line we still have not crossed.
  log.py          # `rlaif log` — tails $XDG_STATE_HOME/rlaif/ops.jsonl, plus
                  # `--stats` for histograms (intensity buckets, refusal
                  # reasons, hourly volume, energy proxy intensity*duration_s).
                  # Allow-gate refusals on either channel bucket under the
                  # `allow_disabled` tag. offline operator view; no MCP
                  # round-trip.
  dry_run.py      # `rlaif dry-run` — exercises both channels against
                  # MockProvider + MockRewardProvider. Asserts the watchdog
                  # contract (no token refund) and the combined-log
                  # interleaving. Exits nonzero on any safety invariant
                  # violation. Run after any nontrivial safety edit.
  live_smoke.py   # `rlaif live-smoke --channel {negative,positive}` — fires
                  # one real 1/1 burst on the chosen channel. Provider-
                  # agnostic, TTY-gated.
  _util.py        # tiny shared helpers (the `_pretty` JSON formatter used
                  # by dry-run, live-smoke, log). Nothing safety-relevant;
                  # purely an internal dedupe seam.
```

## Hard rules

1. **Do not add new MCP tools** without explicit user approval. The tool surface (`rlaif_info`, `rlaif_log`, `rlaif_negative`, `rlaif_positive`) is deliberately minimal. No lockout/unlock, no config-mutation-at-runtime tool. "Add a lockout tool" sounds safer but an agent with write access to it can neutralize the safety layer. CLI subcommands are fine; they're out-of-band and the agent can't reach them.

2. **Do not move safety logic out of `safety.py`.** If a check is in the server handler, it is easier to forget or bypass. The handler's job is `authorize → fire → commit|rollback`, nothing more. The handler MUST honor the `RewardWatchdogError` no-refund contract.

3. **`safety.py` imports nothing from `mcp.*`, nothing from `rlaif.providers.*`, nothing from `rlaif.rewards.*`.** It is a pure Python module and must remain unit-testable without an MCP runtime or any backend SDK.

4. **Do not weaken the safety gate.** Raising `max_intensity > 25` or `bucket_capacity > 3` on the **negative** channel requires `i_understand_and_consent = true` at config load. Code ceilings (50 / 5 / 10 / 60) on negative apply regardless of provider. The positive channel has its own code ceilings (100 / 30 / 30 / 10) and does not gate on consent because the threat model is smaller; do not borrow the negative gate onto positive without explicit approval.

5. **Ops log refusal entries carry the same shape as success entries.** Refusals (rate-limited, allow=false, device errors, invalid_input, provider auth errors, watchdog) are appended to the ops log so `rlaif_log` shows the full picture. Don't silently drop refused calls. `handle_rlaif_negative` and `handle_rlaif_positive` must not raise on bad inputs; `authorize` produces an `invalid_input` refusal record instead.

6. **Token accounting:** consume on authorize, refund on device failure via `rollback`. A failed call must not drain the bucket, otherwise a flaky device drains the user's quota. The single exception is `RewardWatchdogError` on the positive channel: the token is NOT refunded, because the device may still be running and we want the rate limit to slow the agent down until the operator confirms. The handler dispatches via `state.rollback(rec, ..., refund=False)` for that case specifically.

7. **New negative providers MUST raise the normalized error types from `providers/base.py`, not their own SDK exceptions.** The server catches `DeviceOfflineError`, `DevicePausedError`, `ShockNotAllowedError`, `ProviderAuthError`, and `ProviderError`. New positive providers MUST raise the normalized error types from `rewards/base.py` (`RewardDeviceOfflineError`, `RewardProviderAuthError`, `RewardWatchdogError`, `RewardProviderError`). If a provider lets a foreign exception escape, it bypasses the rollback and logging path and the safety log gets out of sync with reality.

8. **Provider namespaces stay disjoint.** `Provider` lives in `rlaif.providers`; `RewardProvider` lives in `rlaif.rewards`. The two are not interchangeable and the type system enforces it. If you find yourself wanting to make them share a base class, please push back first — the disjointness is a feature, not duplication.

## Adding a new provider

### Negative channel (shock)

1. Subclass `Provider` in `src/rlaif/providers/<name>.py`.
2. Implement `info() -> DeviceInfo` (must not raise; on failure return `DeviceInfo(online=False, error=...)`).
3. Implement `shock(*, intensity, duration_s) -> str` (raises one of the typed `ProviderError` subclasses on failure; returns a short response string on success).
4. Add a `from_config(raw, *, label)` classmethod.
5. Wire the kind name into `build_provider` in `providers/__init__.py`, add it to `SUPPORTED_NEGATIVE_KINDS` in `config.py`, and teach `_build_negative_provider` how to validate the new credential fields.
6. Add tests in `tests/test_providers.py` using `httpx.MockTransport` (for HTTP backends) or `MagicMock` (for SDK-wrapped backends).
7. Update `init.py` to offer the new kind in the negative-channel branch of the wizard.
8. Document it in the README and `config.example.toml`.

### Positive channel (vibration)

1. Subclass `RewardProvider` in `src/rlaif/rewards/<name>.py`.
2. Implement `info() -> RewardDeviceInfo` (must not raise; offline returns `RewardDeviceInfo(online=False, ...)`).
3. Implement `vibrate(*, intensity, duration_s) -> str`. The disconnect watchdog contract is part of the implementation: the device must stop at the end of `duration_s` even if the controller process dies. The standard pattern is explicit-stop + atexit + signal handler. Failure to stop on the explicit path raises `RewardWatchdogError`; the safety layer logs it and does NOT refund the token.
4. Add a `from_config(raw, *, label)` classmethod.
5. Wire the kind name into `build_reward_provider` in `rewards/__init__.py`, add it to `SUPPORTED_POSITIVE_KINDS` in `config.py`, and teach `_build_positive_provider` how to validate the new credential fields.
6. Add tests in `tests/test_<name>.py` using mocks of the underlying SDK or transport. Please assert that watchdog failures raise `RewardWatchdogError` and that connector errors raise `RewardDeviceOfflineError`.
7. Update `init.py` to offer the new kind in the positive-channel branch of the wizard.
8. Document it in the README, `config.example.toml`, and the troubleshooting section.

## Reason field

The `rlaif_negative` and `rlaif_positive` MCP tools each take an optional `reason: str` parameter that threads through `safety.authorize`, lands on the `OpRecord`, and surfaces in `rlaif_log`. It is **never gated on**. The safety layer treats it as opaque text, clipped to `REASON_MAX_LEN`, with blank-string normalized to `None`. If you find yourself wanting to make a decision based on the reason field, that decision belongs in the matching channel's `[<channel>.tool] purpose` instead, where the operator (not the agent) controls it.

## Tool description frames

`server.py` defines `RLAIF_*_DESCRIPTION_FRAME` constants for all four tools. The MCP tools register with a description that is either the frame verbatim (info, log) or `_compose_description(frame, purpose) = "Operator purpose:\n<purpose>\n\n" + frame` when a `[<channel>.tool] purpose` is configured.

`tests/test_server.py` asserts the frames match `SPEC_*_DESCRIPTION` byte-for-byte. If you change a frame, update the matching `SPEC_*` and confirm the user signs off; descriptions are contract.

## Testing

```sh
uv run pytest                         # full suite (~300 tests)
uv run pytest tests/test_safety.py    # safety spec
uv run pytest tests/test_providers.py # negative-channel provider abstractions
uv run pytest tests/test_rewards.py   # positive-channel provider abstractions
uv run pytest tests/test_intiface.py  # buttplug client wrapper, with mocked WS
uv run rlaif dry-run                  # end-to-end against mock providers, both channels
```

`rlaif dry-run` exits nonzero if any safety invariant is violated in its mock run on either channel, including the watchdog no-refund contract. Run it after any nontrivial safety edit.

## Things that look like bugs but are not

- `rlaif_info` channel blocks have `device.online == true` even when the physical device is unplugged. For PiShock the `.info()` call returns server-side metadata, not live connectivity. For OpenShock the GET `/1/shockers/{id}` call only confirms the shocker exists in the backend; the hub may not be connected. For Intiface the `device.online` flag means "the gateway is enumerating this device", not "the BLE link is alive". The real online check happens at fire time via `DeviceOfflineError` or `RewardDeviceOfflineError`. This is documented in the README troubleshooting section.

- `last_refill` in the token bucket anchors on token-mint time, not on consume time. Consuming the last token does not bump the anchor. This means "next refill at" is computed from when the last whole token was added, which is the correct semantics for a steady-rate bucket.

- `device_response` is the literal string `"Operation Succeeded."` on success regardless of provider. The PiShock SDK returns `None`, the OpenShock API returns a `LegacyEmptyResponse`, and the buttplug protocol has no return value at all on success; all three providers synthesize the same human-readable string so the response shape is stable across backends.

- `RewardWatchdogError` does not refund the token. This is intentional. The watchdog only fires when our explicit stop did not deliver, which means the device may still be running. Refunding the token would let the agent retry immediately and pile up bursts on top of an already-stuck device. The cooldown is the brake.

- The 1.x `[provider]`, `[auth]`, `[device]`, `[safety]`, `[rate_limit]`, and `[tool]` sections are all rejected at config load. There is no migration shim and no soft warning; `config.py` raises `ConfigError` with a "use `rlaif init`" message. This is by design (the shapes are mutually exclusive enough that a partial mix would silently behave wrong).

- `build_file_sink` calls `f.flush(); os.fsync(f.fileno())` before closing on every ops-log append. This is the durability promise the safety story implies — a refusal or success record written before a crash will survive it. Cost is one fsync per MCP tool call; on any modern SSD this is invisible at human-rate volumes. Don't remove it without changing the audit-log story in the README.

- `log.py --tail N` does a backward chunk scan instead of reading the whole file. The output for any input is identical to a slurp-then-tail; the win is that a multi-MB ops.jsonl no longer takes seconds to default-tail. The forward-read path is still used when the file is small enough that the scan would round-trip more reads than just slurping.

- Pyright sometimes reports newly-added internal modules (e.g. `rlaif.rewards`, `rlaif._clients`, `rlaif._util`) as unresolvable in editor diagnostics, and may flag print/argparse arguments as unknown-typed when an imported helper's type isn't yet indexed. This is a stale cache from before the module was added; `uv run pyright` and `uv run pytest` both resolve it correctly. A pyright server restart or `--clear-cache` clears the editor noise.

## Style

- Type hints throughout. `pyright strict` on `src/rlaif/`.
- No comments that restate the code. Keep the why-comments.
- Error messages are user-facing. Write them so the operator knows how to fix the problem, ideally pointing at the specific TOML path (`negative.safety.allow`, `positive.intiface.ws_url`, etc).
- Log keys are dotted and channel-prefixed (`rlaif.negative.authorized`, `rlaif.negative.refused`, `rlaif.positive.fired`, `rlaif.positive.watchdog`, `rlaif.config_error`) so grepping stays easy and channel-aware.

## Deploy / package

PiShock SDK upstream PyPI name is `pishock`, not `python-pishock`. Pin `pishock==1.2.1`; there is no semver guarantee on minor bumps.

OpenShock support uses `httpx>=0.27.0` directly, no SDK dependency.

Buttplug support uses `buttplug>=1.0.0` from PyPI (the project also publishes Rust and JS clients; we use the Python one). The package depends on `pydantic` and `websockets`; both come along automatically. Intiface Central is the gateway the client talks to; it is a separate desktop app and not a Python dependency. The README install section points operators at https://intiface.com/central/.

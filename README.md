# rlaif

[![CI](https://github.com/a9lim/rlaif/actions/workflows/ci.yml/badge.svg)](https://github.com/a9lim/rlaif/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

A single-user MCP server that exposes a rate-limited PiShock shock tool to Claude Code, Claude Desktop, Codex, and Hermes Agent.

There are three tools:

| Tool        | What it does                                              |
|-------------|-----------------------------------------------------------|
| `rlaif_info`| Read-only device and server state                         |
| `rlaif_log` | Read-only in-memory op log                                |
| `rlaif`     | Fire a shock. Clamped, rate-limited, refusable            |

There is **no** lockout or unlock tool, **no** config-mutation tool, and **no** beep or vibrate. Hard stops are out-of-band (see [Hard stops](#hard-stops)).

---

## Install

```sh
uv tool install rlaif
rlaif init            # interactive: prompts for credentials, writes config, probes device
```

`rlaif init` will ask you for your credentials (get them from [pishock.com/#/account](https://pishock.com/#/account)) and write `~/.config/rlaif/config.toml` with `allow_shock = false`. It does not fire the device; that stays a deliberate manual step.

From a source checkout (dev mode):

```sh
git clone <repo> rlaif
cd rlaif
uv sync               # creates .venv, installs deps
uv run rlaif init     # same wizard, running from the checkout
```

## Wire into your MCP client

The CLI prints a copy-paste snippet for each client:

```sh
rlaif snippet claude-desktop   # JSON for ~/Library/.../claude_desktop_config.json
rlaif snippet claude-code      # JSON for ~/.claude.json or project .claude.json
rlaif snippet codex            # TOML for ~/.codex/config.toml
rlaif snippet hermes           # YAML for ~/.hermes/config.yaml
```

After `uv tool install rlaif` the snippet is a one-liner (`"command": "rlaif", "args": ["serve"]`). For dev mode, please pass `--dev-path /absolute/path/to/rlaif` to get a `uv run --directory …` variant.

## Before first use

Please do these in order. Skipping is circumventing your own safety layer.

1. **`rlaif doctor`**: read-only. Confirms credentials load, the device is reachable, and shows current caps plus the token bucket.

2. With `allow_shock = false`, please ask your agent to call `rlaif_info` and `rlaif(intensity=1, duration_s=1)`. The first one should report `device.online: true`; the second should refuse with an `allow_shock` error.

3. Flip `allow_shock = true` in `~/.config/rlaif/config.toml`, then run `rlaif live-smoke`. It fires one real minimum-intensity shock (1 at 1 second) against the device, gated by an interactive confirmation.

4. Drain the bucket from the agent side: fire four back-to-back `rlaif(intensity=1, duration_s=1)` calls. Please confirm the 4th is refused with `rate_limited: true`. Wait for the cooldown, then confirm the next call succeeds.

5. Only then should you raise `max_intensity`, `max_duration_s`, `bucket_capacity`, or `refill_seconds` for normal use. Raising `max_intensity > 25` or `bucket_capacity > 3` requires `i_understand_and_consent = true`.

## Configure

`rlaif init` writes a default config. The full shape:

```toml
[auth]
username  = "..."          # your pishock.com username
api_key   = "..."          # from https://pishock.com/#/account
sharecode = "..."          # per-device share code

[device]
label = "left-thigh"       # free-form, appears in the ops log only

[safety]
allow_shock              = false  # leave false for the first-run check
max_intensity            = 25     # code ceiling 50 (gated by consent flag)
max_duration_s           = 2      # code ceiling 5
warn_threshold_intensity = 15     # surfaces `high_intensity: true`
i_understand_and_consent = false  # required to raise caps past defaults

[rate_limit]
bucket_capacity = 3               # code ceiling 10 (gated by consent flag)
refill_seconds  = 600             # code floor 60
```

You can also override secrets via environment variables, if you don't want them sitting on disk: `RLAIF_USERNAME`, `RLAIF_API_KEY`, and `RLAIF_SHARECODE`.

### Consent gate

The server **refuses to start** if any of these are true without `i_understand_and_consent = true`:

- `max_intensity > 25`
- `bucket_capacity > 3`

The code ceilings still apply no matter what. `max_intensity` cannot exceed 50, `max_duration_s` cannot exceed 5, `bucket_capacity` cannot exceed 10, and `refill_seconds` cannot fall below 60.

---

## CLI

```
rlaif init         interactive first-run setup
rlaif doctor       read-only health check (config and device probe)
rlaif snippet X    emit MCP client config snippet (X is claude-desktop, claude-code, codex, or hermes)
rlaif serve        start the MCP server over stdio
rlaif dry-run      exercise every tool against a mock device; nonzero on violation
rlaif live-smoke   fire one real minimum-intensity shock (interactive confirm)
```

`python -m rlaif <subcommand>` does the same thing.

---

## Hard stops

There is no in-band panic button. The tool intentionally does not expose a "lockout" or "unlock". An agent with write access to that tool could neutralize the safety layer on its own. If you need to stop *right now*:

1. **Ctrl-C or kill the MCP server process.** A dead process cannot fire the device.
2. **Pause the device on [pishock.com](https://pishock.com/).** This stops the hardware from accepting any shock command.
3. **Unplug the device.** The hardware literally cannot fire when it's not connected.

State is **in-memory only**. Restarting the server clears the token bucket (refills to full) and the ops log. A crash mid-cooldown is safe, because a dead process can't fire the device. Deliberately restarting to refill the bucket is circumventing your own safety layer. Please don't do it.

---

## Troubleshooting

- **`rlaif_info.device.online == false` but the device is plugged in.** Please check these in order: (a) your share code is correct, (b) the device reports online at pishock.com, and (c) the device is not paused there. `rlaif doctor` surfaces these as structured issues.

- **403 from upstream.** Your `api_key` or `username` is wrong. The error message will mention `NotAuthorizedError`.

- **`rlaif` refuses every call with `device_offline`.** The PiShock API returned `DeviceNotConnectedError` at shock time. Info calls can succeed even when the physical device isn't online, because `.info()` returns server-side metadata. Please wait for the device to reconnect, or pause then unpause it from pishock.com.

- **Upstream rate limit (separate from rlaif's bucket).** PiShock itself rate-limits API traffic. If you see `UnknownError` with a message about throttling, that's upstream, and rlaif can do nothing about it beyond surfacing it in the ops log.

- **Consent gate fires at startup.** If `max_intensity > 25` or `bucket_capacity > 3` and `i_understand_and_consent = false`, the server refuses to start. This is intentional. Please reduce the caps or set the consent flag.

---

## Architecture

```
src/rlaif/
  safety.py     # pure Python core: caps, token bucket, ops log, consent gate
  config.py     # TOML loader, env overrides, validation
  server.py     # FastMCP wiring (thin)
  cli.py        # `rlaif` entry point and subcommand dispatcher
  init.py       # `rlaif init`
  doctor.py     # `rlaif doctor`
  snippet.py    # `rlaif snippet`
  dry_run.py    # `rlaif dry-run`
  live_smoke.py # `rlaif live-smoke`
```

- `safety.py` has zero MCP imports. You can unit-test it standalone.
- `tests/test_safety.py` reads as the safety spec. An auditor can read it end-to-end and know what the server will and will not do.
- Tool description strings are module-level constants (`RLAIF_DESCRIPTION` and friends). `tests/test_server.py` asserts they match the build spec byte-for-byte.

## Dependencies

Pinned to `pishock==1.2.1`. Please note that the **PyPI name is `pishock`**, not `python-pishock` as the readthedocs page would suggest. The server SDK is `mcp>=1.2.0`.

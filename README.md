# rlaif

[![CI](https://github.com/a9lim/rlaif/actions/workflows/ci.yml/badge.svg)](https://github.com/a9lim/rlaif/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/rlaif-mcp)](https://pypi.org/project/rlaif-mcp/)
[![Downloads](https://img.shields.io/pypi/dm/rlaif-mcp)](https://pypi.org/project/rlaif-mcp/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://pypi.org/project/rlaif-mcp/)

This is a single-user MCP server that provides a shock tool. PiShock and OpenShock are both supported.

There are three tools:

| Tool        | Function                                            |
|-------------|-----------------------------------------------------|
| `rlaif_info`| Read-only device and server state                   |
| `rlaif_log` | Read-only log                                       |
| `rlaif`     | Fire a shock (intensity, duration, reason) |

There is no tool to change the config, it is set at launch. The only purpose of this project is for your agent to be able to zap you.

---

## Reporting issues

If you notice any errors while using the program, please update to the most recent version and reinstall the hooks. If it still persists, please open an issue. This project is a work in progress and I am actively finding and fixing bugs.

---

## Install

The PyPI distribution name is `rlaif-mcp` (the bare `rlaif` name is taken; transfer pending). The import and CLI command are still just `rlaif`.

```sh
uv tool install rlaif-mcp
rlaif init            # interactive: pick provider, credentials, config, doctor, MCP snippet
```

`rlaif init` will ask you which backend to use (PiShock or OpenShock) and prompt for the matching credentials. It then writes `~/.config/rlaif/config.toml` with `allow_shock = false`, runs `rlaif doctor` to probe the device, and offers to emit an MCP client snippet for you. It does not fire the device on startup.

PiShock credentials come from [pishock.com/#/account](https://pishock.com/#/account). OpenShock tokens come from your dashboard at [openshock.app/#/dashboard/tokens](https://openshock.app/#/dashboard/tokens), or your self-hosted equivalent. The `shocker_id` for OpenShock is the UUID of the specific shocker.

From a source checkout:

```sh
git clone <repo> rlaif
cd rlaif
uv sync               # creates .venv, installs deps
uv run rlaif init     # same wizard, running from the checkout
```

## Connect MCP client

There are two ways to register rlaif: auto-install (8 clients with dedicated config files) or copy-paste snippet (all 10 clients).

### Auto-install

```sh
rlaif install claude-desktop    # JSON: ~/Library/.../claude_desktop_config.json
rlaif install claude-code       # JSON: ~/.claude.json
rlaif install cursor            # JSON: ~/.cursor/mcp.json
rlaif install windsurf          # JSON: ~/.codeium/windsurf/mcp_config.json
rlaif install antigravity       # JSON: ~/.gemini/antigravity/mcp_config.json
rlaif install opencode          # JSON: ~/.config/opencode/opencode.json
rlaif install codex             # TOML: ~/.codex/config.toml (tomlkit round-trip)
rlaif install hermes            # YAML: ~/.hermes/config.yaml (ruamel.yaml round-trip)
```

Install atomically merges in an `rlaif` entry. A single `<file>.rlaif.bak` is kept. If a different `rlaif` entry already exists, install refuses unless you pass `--force`. Pass `--dry-run` to preview without writing. `rlaif uninstall <client>` removes the entry the same way.

opencode supports both `opencode.json` and `opencode.jsonc`. Auto-install only handles plain JSON; if your config is the JSONC variant, install will detect the parse failure and tell you to use `rlaif snippet opencode` instead.

### Snippet (paste manually)

The remaining 2 clients use JSONC inside a multi-purpose settings file (vscode, zed). For those, use `snippet`:

```sh
rlaif snippet claude-desktop   # JSON for ~/Library/.../claude_desktop_config.json
rlaif snippet claude-code      # JSON for ~/.claude.json or project .claude.json
rlaif snippet codex            # TOML for ~/.codex/config.toml
rlaif snippet hermes           # YAML for ~/.hermes/config.yaml
rlaif snippet antigravity      # JSON for ~/.gemini/antigravity/mcp_config.json
rlaif snippet opencode         # JSON for opencode.json or ~/.config/opencode/opencode.json
rlaif snippet cursor           # JSON for ~/.cursor/mcp.json or .cursor/mcp.json
rlaif snippet windsurf         # JSON for ~/.codeium/windsurf/mcp_config.json
rlaif snippet vscode           # JSON for .vscode/mcp.json or user mcp.json
rlaif snippet zed              # JSON fragment for ~/.config/zed/settings.json
```

After `uv tool install rlaif-mcp` the snippet is a one-liner: `"command": "rlaif", "args": ["serve"]`. For dev mode, please pass `--dev-path /absolute/path/to/rlaif` to get a `uv run --directory …` variant. The same flag works on `install`.

## Before use

Please do these in order, this is for safety. The checklist is the same regardless of which provider you picked.

1. **`rlaif doctor`**: Confirms credentials load and the shocker is reachable.

2. With `allow_shock = false`, please ask your agent to call `rlaif_info` and `rlaif(intensity=1, duration_s=1)`. The first one should report `device.online: true`; the second should refuse with an `allow_shock` error.

3. Set `allow_shock = true` in `~/.config/rlaif/config.toml`, then run `rlaif live-smoke`. It fires a real minimum-intensity shock (1 at 1 second) on the device, gated by a confirmation.

4. Ask the agent to fire four consecutive `rlaif(intensity=1, duration_s=1)` calls. You should confirm the fourth is refused with `rate_limited: true`. Wait for the cooldown, then confirm the next call succeeds.

5. Only then you should raise `max_intensity`, `max_duration_s`, `bucket_capacity`, or `refill_seconds` for normal use. If you want to set `max_intensity > 25` or `bucket_capacity > 3` you have to enable `i_understand_and_consent = true`.

## Configure

`rlaif init` writes a default config. The shape is:

```toml
[provider]
kind = "pishock"          # or "openshock"

[provider.pishock]
username  = "..."         # your pishock.com username
api_key   = "..."         # from https://pishock.com/#/account
sharecode = "..."         # per-device share code

# [provider.openshock]
# api_token  = "..."        # from https://openshock.app/#/dashboard/tokens
# shocker_id = "..."        # uuid of the specific shocker
# base_url   = "https://api.openshock.app"  # optional; only set if self-hosting

[device]
label = "left-thigh"      # free-form, appears in the ops log only

[tool]
# Optional preamble prepended to the rlaif tool description so the agent
# sees the operator's intended use. Empty/omitted preserves the
# spec-only description.
# purpose = "Use rlaif to enforce focus during pomodoros: shock me if I switch to twitter."

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

You can also override the secrets via environment variables. PiShock: `RLAIF_USERNAME`, `RLAIF_API_KEY`, `RLAIF_SHARECODE`. OpenShock: `RLAIF_OPENSHOCK_TOKEN`, `RLAIF_OPENSHOCK_SHOCKER_ID`, `RLAIF_OPENSHOCK_BASE_URL`.

### Tool purpose

`[tool] purpose = "..."` is an operator-authored preamble prepended to the `rlaif` tool description. It does not change any safety behavior; it just tells the agent *when* to shock. The descriptions for `rlaif_info` and `rlaif_log` are not affected.

```toml
[tool]
purpose = "Use rlaif to enforce focus during pomodoros: shock me if I switch to twitter or instagram during a session."
```

### Reason

The `rlaif` tool takes an optional `reason: str` parameter. It is logged on the op record and surfaces in `rlaif_log`, but it never gates the call. Please use it freely so the on-disk log is readable later.

```jsonc
// rlaif(intensity=8, duration_s=1, reason="agent saw twitter open during a focus block")
```

---

## Safety gate

Rlaif refuses to start if either of these is set without `i_understand_and_consent = true` in the config:

- `max_intensity > 25`
- `bucket_capacity > 3`

The defaults are meant to stay conservative. The flag makes sure that raising them is an explicit step. Please read this section before you flip it.

| Setting | Default | Gated | Code ceiling |
|---------|---------|-------|--------------|
| `max_intensity` | 25 | yes (above 25) | 50 |
| `max_duration_s` | 2 | no | 5 |
| `bucket_capacity` | 3 | yes (above 3) | 10 |
| `refill_seconds` | 600 | no | 60 (floor, not ceiling) |

The code ceilings apply no matter what the config says or which provider you use. You cannot raise `max_intensity` above 50 or `bucket_capacity` above 10 by editing the config, and `refill_seconds` cannot go below 60; the server will refuse to start. Please do not attempt to patch these constants out.

At default settings the worst case is a burst of 3 shocks at intensity 25 for 2 seconds each, with a 10-minute cooldown per additional shock after the bucket empties. At fully raised settings the worst case is 10 shocks at intensity 50 for 5 seconds each, with a 1-minute cooldown per additional shock. Please keep the caps at what you are comfortable.

---

## CLI

```
rlaif init         interactive first-run setup (writes config, runs doctor, offers snippet)
rlaif doctor       read-only health check (config and device probe, provider-agnostic)
rlaif snippet X    emit MCP client config snippet (X is one of the 10 clients)
rlaif install X    auto-write rlaif into a supported MCP client config (X is one of the 8 auto-install clients)
rlaif uninstall X  remove rlaif from one of the same 8 supported configs
rlaif serve        start the MCP server over stdio
rlaif log          tail the on-disk ops log (default: last 10 entries, --tail N to change)
rlaif log --stats  print rolling histograms (intensity buckets, refusal reasons, hourly volume)
rlaif dry-run      exercise every tool against a mock provider; nonzero on violation
rlaif live-smoke   fire one real minimum-intensity shock (interactive confirm)
```

`python -m rlaif <subcommand>` does the same thing.

---

## Safety

There is no built in stop button, so an agent may shock you too much. If you need to stop it immediately:

1. **Ctrl-C or kill the MCP server process.**
2. **Pause the device** at [pishock.com](https://pishock.com/) or your OpenShock dashboard.
3. **Unplug the device.**

Restarting the server clears the cooldowns. I would strongly recommend against deliberately restarting the server to skip the cooldown.

---

## Troubleshooting

- **`rlaif_info.device.online == false` but the device is on.** Please check these potential issues. PiShock: (a) your sharecode is correct, (b) the device is online at pishock.com, (c) the device is not paused there. OpenShock: (a) your `api_token` is valid and not expired, (b) the `shocker_id` matches a shocker your token has permission for, (c) the shocker is not paused on the OpenShock dashboard. `rlaif doctor` will display these issues if they are present.

- **403 from PiShock.** Your `api_key` or `username` is wrong. The error message should mention `NotAuthorizedError`.

- **401 or 403 from OpenShock.** Your `api_token` is wrong, expired, or missing the `Shockers.Use` permission. Generate a new token on the dashboard with the right permission scope.

- **404 from OpenShock on shock.** The `shocker_id` is unknown or not shared with your token. rlaif treats this as `device_offline` so the safety layer refunds the token; please double-check the UUID.

- **`rlaif` refuses every call with `device_offline`.** PiShock: the API returned `DeviceNotConnectedError`. Info calls can succeed when the physical device isn't online, because `.info()` returns server-side metadata. OpenShock: the same gotcha applies; `info()` only confirms the shocker exists in the backend, not that the hub is connected. Please wait for the device to reconnect, or pause and unpause it at the provider.

- **Upstream rate limit (separate from rlaif's bucket).** PiShock and OpenShock both rate-limit API traffic on their side. If you see an error mentioning throttling, that's from upstream and rlaif can do nothing about it.

- **Safety gate fires at startup.** If `max_intensity > 25` or `bucket_capacity > 3` and `i_understand_and_consent = false`, the server refuses to start. This is intentional. Please reduce the caps or enable the consent flag.

- **Config path on Windows.** The default is `%USERPROFILE%\.config\rlaif\config.toml`, not `%APPDATA%`. If you set `XDG_CONFIG_HOME`, rlaif uses that instead. `rlaif init` writes to whichever path resolves, so please run it rather than creating the file by hand.

- **Config file permissions.** `rlaif init` writes the config with mode `0600` so other users on the same machine cannot read your secrets. Rlaif does not re-check permissions on load, so if you edit the file and widen the mode, please chmod it back: `chmod 600 ~/.config/rlaif/config.toml`.

- **Env vars override the config file.** `RLAIF_USERNAME`, `RLAIF_API_KEY`, `RLAIF_SHARECODE` for PiShock; `RLAIF_OPENSHOCK_TOKEN`, `RLAIF_OPENSHOCK_SHOCKER_ID`, `RLAIF_OPENSHOCK_BASE_URL` for OpenShock. They take precedence over the values in the matching `[provider.<kind>]` block. If the credentials in the config file look right but rlaif seems to be using different ones, please check whether one of these env vars is set in your shell or in your MCP client's launch environment.

- **Both `[provider]` and `[auth]` in the same file.** rlaif refuses to start because mixing the new and legacy schemas is an accident waiting to happen. Please remove the `[auth]` block and use `[provider.pishock]` instead.

---

## Architecture

```
src/rlaif/
  safety.py             # pure Python core: caps, token bucket, ops log, safety gate
  config.py             # TOML loader, env overrides, provider resolution
  providers/
    base.py             # Provider ABC, normalized error taxonomy, DeviceInfo
    pishock.py          # PiShock backend (pishock package)
    openshock.py        # OpenShock REST backend (httpx; cloud or self-hosted)
    mock.py             # in-memory provider for tests and dry-run
  server.py             # FastMCP wiring (thin); reason field; purpose preamble
  cli.py                # `rlaif` entry point and subcommand dispatcher
  init.py               # `rlaif init`
  doctor.py             # `rlaif doctor`
  snippet.py            # `rlaif snippet`
  installer.py          # `rlaif install` / `rlaif uninstall`
  log.py                # `rlaif log` (tail and --stats)
  dry_run.py            # `rlaif dry-run`
  live_smoke.py         # `rlaif live-smoke`
```

The safety layer is provider-blind. Adding a new backend means writing a `Provider` subclass that returns a `DeviceInfo` from `.info()` and raises one of the typed `ProviderError` subclasses from `.shock()`; no other file needs to change.

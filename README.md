# rlaif

[![CI](https://github.com/a9lim/rlaif/actions/workflows/ci.yml/badge.svg)](https://github.com/a9lim/rlaif/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/rlaif-mcp)](https://pypi.org/project/rlaif-mcp/)
[![Downloads](https://img.shields.io/pypi/dm/rlaif-mcp)](https://pypi.org/project/rlaif-mcp/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://pypi.org/project/rlaif-mcp/)

This is a single-user MCP server that gives your agent a negative and/or a positive feedback channel. The purpose of this project is to allow your agent to provide immediate feedback on your behavior.

There are up to four tools:

| Tool              | Function                                                       |
|-------------------|----------------------------------------------------------------|
| `rlaif_info`      | Read-only device and server state across both channels         |
| `rlaif_log`       | Read-only log across both channels by timestamp                |
| `negative`        | Negative feedback             (intensity, duration_s, reason). |
| `positive`        | Positive feedback             (intensity, duration_s, reason). |

`negative` and `positive` are only registered when their respective fields are configured. There is no internal tool to change the config, it is set at launch. Only one stimulus runs at a time across both channels (see [One stimulus at a time](#one-stimulus-at-a-time)).

## 3.0 release

Version 3.0 moves to version 2 of the MCP Python SDK. The config file does not change, but a few things you will notice do:

- **One stimulus at a time.** Both channels now share one fire gate. While a shock or vibration is running, and for a short settle window after it, every `negative` or `positive` call is refused with `error: "busy: …"`. A refused call spends no token, is logged, and is never queued. Back-to-back bursts are no longer possible; space them out.
- **The fire tools are named `negative` and `positive`.** 2.0.x called them `rlaif_negative` and `rlaif_positive`, so update any client allowlists or prompts that name them.
- **PiShock uses the v3 API.** PiShock shut down the `do.pishock.com` API that 2.0.x used, so the PiShock provider in 2.0.x cannot fire. rlaif now talks to `api.pishock.com` directly. Your hub has to be on V3 firmware, and the share code has to be one your account has claimed (see [Troubleshooting](#negative-channel-1)).
- **Client list.** `windsurf` is now `devin` (Windsurf became Devin Desktop). `antigravity` now writes `~/.gemini/config/mcp_config.json`, which the Antigravity IDE and the new `agy` CLI share. New clients are `copilot`, `grok`, `kiro`, `junie`, `droid`, and `lmstudio`. If you had installed into Windsurf or the old `~/.gemini/antigravity/` path, run `rlaif install devin` or `rlaif install antigravity` again and delete the stale `rlaif` entry by hand.

Coming from 1.x: the 1.x config schema is gone, so please run `rlaif init` to write a fresh config and copy your credentials over. 1.x `ops.jsonl` entries lack the `channel` field and will not parse cleanly under `rlaif log --stats`; please archive the old log if you want to keep it.

## Reporting issues

If you notice any errors while using the program, please update to the most recent version and reinstall the hooks. If it still persists, please open an issue. This project is a work in progress and I am actively finding and fixing bugs.

## Install

The PyPI distribution name is `rlaif-mcp` (the bare `rlaif` name is currently taken). The import and CLI command are still just `rlaif`.

```sh
python -m pip install rlaif-mcp
rlaif init            # interactive: pick channels, credentials, config, doctor, auto-install
```

`rlaif init` will ask which channels you want and prompt for the matching credentials per channel. It then writes `~/.config/rlaif/config.toml`, runs `rlaif doctor` to probe each device, and lets you install the MCP server for the supported clients. Alternatively, for less-supported clients, it prints a paste-into snippet. 

Negative feedback credentials come from [login.pishock.com/account](https://login.pishock.com/account), [openshock.app/#/dashboard/tokens](https://openshock.app/#/dashboard/tokens), or your self-hosted equivalent. Both backends use the same field names: `api_token` is the per-account token, `shocker_id` is the per-device identifier (the share code on PiShock, the UUID on OpenShock). The positive channel needs Intiface Central running locally on `ws://localhost:12345` with your device paired; please install it from [intiface.com/central](https://intiface.com/central/) before running `rlaif live-smoke --channel positive`.

From a source checkout:

```sh
git clone <repo> rlaif
cd rlaif
python -m pip install -e ".[dev]"
rlaif init            # same wizard, running from the checkout
```

## Connect MCP client

There are two ways to register rlaif: auto-install (14 clients) or copy-paste snippet (all 16 clients).

### Auto-install

```sh
rlaif install                   # auto-detect every supported config present and install into all
rlaif install claude-desktop    # JSON: ~/Library/.../claude_desktop_config.json
rlaif install claude-code       # JSON: ~/.claude.json
rlaif install codex             # TOML: ~/.codex/config.toml (tomlkit round-trip)
rlaif install copilot           # JSON: ~/.copilot/mcp-config.json ($COPILOT_HOME honored; Copilot CLI, also read by VS Code)
rlaif install grok              # TOML: ~/.grok/config.toml (Grok Build)
rlaif install antigravity       # JSON: ~/.gemini/config/mcp_config.json (Antigravity IDE + `agy` CLI)
rlaif install cursor            # JSON: ~/.cursor/mcp.json
rlaif install devin             # JSON: ~/.config/devin/mcp_config.json (formerly Windsurf; $XDG_CONFIG_HOME honored)
rlaif install kiro              # JSON: ~/.kiro/settings/mcp.json (Kiro IDE + kiro-cli)
rlaif install junie             # JSON: ~/.junie/mcp/mcp.json (JetBrains plugin + CLI)
rlaif install droid             # JSON: ~/.factory/mcp.json (Factory Droid)
rlaif install opencode          # JSON: ~/.config/opencode/opencode.json
rlaif install hermes            # YAML: ~/.hermes/config.yaml (ruamel.yaml round-trip)
rlaif install lmstudio          # JSON: ~/.lmstudio/mcp.json
```

Install atomically merges in an `rlaif` entry. A single `<file>.rlaif.bak` is kept. If a different `rlaif` entry already exists, install refuses unless you pass `--force`. Pass `--dry-run` to preview without writing. `rlaif uninstall <client>` removes the entry the same way; `rlaif uninstall` with no client auto-detects every supported config that has a `rlaif` entry and removes it from each.

Autodetect uses "config file already exists on disk" as the signal — a freshly-installed client that has not yet written its config is invisible to autodetect, and `rlaif install <client>` is the explicit escape hatch. `--dev-path`, `--dry-run`, and `--force` all flow through to every detected client. A conflict on one client (existing different `rlaif` entry without `--force`) surfaces in the exit code but does not block the rest from installing.

### Snippet (paste manually)

The remaining 2 clients use JSONC inside a multi-purpose settings file (vscode, zed). For those, use `snippet`. It also works for every auto-install client if you would rather paste the entry yourself:

```sh
rlaif snippet vscode           # JSON for .vscode/mcp.json (workspace); for user level, `rlaif install copilot`
rlaif snippet zed              # JSON fragment for ~/.config/zed/settings.json
rlaif snippet <client>         # any of the 14 auto-install clients above
```

After installation the snippet is a one-liner: `"command": "rlaif", "args": ["serve"]`.

## Before use

Please do these in order, this is for safety. Run the checklist for each channel you configured.

### Negative channel 

1. **`rlaif doctor`**: Confirms credentials load and the device is reachable.

2. With `[negative.safety] allow = false`, please ask your agent to call `rlaif_info` and `negative(intensity=1, duration_s=1)`. The first should report `negative.device.online: true`; the second should refuse with a `negative.safety.allow` error.

3. Set `[negative.safety] allow = true` in `~/.config/rlaif/config.toml`, then run `rlaif live-smoke --channel negative`. It fires a real minimum-intensity negative trigger (1 at 1 second), gated by an interactive confirmation.

### Positive channel

1. Start Intiface Central from your applications, pair your device, and confirm it shows up in the Intiface devices panel.

2. **`rlaif doctor`**: Confirms the gateway is reachable and the configured device is enumerated.

3. With `[positive.safety] allow = false`, please ask your agent to call `rlaif_info` and `positive(intensity=1, duration_s=1)`. The first should report `positive.device.online: true` with an `actuators` count; the second should refuse with a `positive.safety.allow` error.

4. Set `[positive.safety] allow = true`, then run `rlaif live-smoke --channel positive`. It fires a real minimum-intensity positive trigger (1 at 1 second), gated by an interactive confirmation.

Only then you should raise the values in the configuration for normal use. If you want to set `max_intensity > 25` or `bucket_capacity > 3` on the negative channel you have to enable `i_understand_and_consent = true`. The positive channel does not require `i_understand_and_consent`, because the device cannot hurt you. 

## Configure

`rlaif init` writes a default config. The shape is:

```toml
[negative]
kind  = "pishock"            # or "openshock"
label = "front"             # free-form, appears in the ops log only

[negative.pishock]
username   = "..."           # your pishock.com username (checked against the API key's account)
api_token  = "..."           # from https://login.pishock.com/account (the "API key")
shocker_id = "..."           # per-device share code, claimed by that account

# [negative.openshock]
# api_token  = "..."           # from https://openshock.app/#/dashboard/tokens
# shocker_id = "..."           # uuid of the specific shocker
# base_url   = "https://api.openshock.app"  # optional; only set if self-hosting

[negative.safety]
allow                    = false  # leave false for the first-run check
max_intensity            = 25     # code ceiling 50 (gated by consent flag)
max_duration_s           = 2      # code ceiling 5
warn_threshold_intensity = 15     # surfaces `high_intensity: true`
bucket_capacity          = 3      # code ceiling 10 (gated by consent flag)
refill_seconds           = 600    # code floor 60
i_understand_and_consent = false  # required to raise caps past defaults

[negative.tool]
# Optional preamble prepended to the `negative` tool description so the
# agent sees the operator's intended use.
# purpose = "Use the negative tool to enforce focus during pomodoros: shock me if I switch to twitter."

[positive]
kind  = "intiface"
label = "rear"

[positive.intiface]
base_url    = "ws://localhost:12345"    # default; override only if Intiface runs elsewhere
device_name = "..."                     # which paired device (exact name or display name)

[positive.safety]
allow           = false
max_intensity   = 75                    # code ceiling 100
max_duration_s  = 5                     # code ceiling 30
bucket_capacity = 5                     # code ceiling 30
refill_seconds  = 30                    # code floor 10

[positive.tool]
# purpose = "Use the positive tool to reward me when i finish a focused work block."
```

You can also override secrets and endpoints via environment variables. PiShock: `RLAIF_PISHOCK_USERNAME`, `RLAIF_PISHOCK_API_TOKEN`, `RLAIF_PISHOCK_SHOCKER_ID`. OpenShock: `RLAIF_OPENSHOCK_API_TOKEN`, `RLAIF_OPENSHOCK_SHOCKER_ID`, `RLAIF_OPENSHOCK_BASE_URL`. Intiface: `RLAIF_INTIFACE_BASE_URL`. Env values win over the file when both are present.

### Tool purpose

`[negative.tool] purpose = "..."` is an operator-authored preamble prepended to the `negative` tool description. `[positive.tool] purpose = "..."` does the same thing for `positive`. They do not change any safety behavior; they just tell the agent when to fire that channel.

### Reason

The `negative` and `positive` tools each take an optional `reason: str` parameter. It is logged on the op record and appears in `rlaif_log`. 

```jsonc
// negative(intensity=8, duration_s=1, reason="agent saw twitter open during a focus block")
// positive(intensity=50, duration_s=2, reason="finished pomodoro without context-switching")
```

---

## Safety gate

Rlaif refuses to start if either of these is set on the negative channel without `i_understand_and_consent = true`:

- `[negative.safety] max_intensity > 25`
- `[negative.safety] bucket_capacity > 3`

The defaults are meant to stay conservative. The flag makes sure that raising them is an explicit step. Please read this section before you flip it.

| Setting (negative) | Default | Gated | Code ceiling |
|--------------------|---------|-------|--------------|
| `max_intensity` | 25 | yes (above 25) | 50 |
| `max_duration_s` | 2 | no | 5 |
| `bucket_capacity` | 3 | yes (above 3) | 10 |
| `refill_seconds` | 600 | no | 60 (floor) |

| Setting (positive) | Default | Gated | Code ceiling |
|--------------------|---------|-------|--------------|
| `max_intensity` | 75 | no | 100 |
| `max_duration_s` | 5 | no | 30 |
| `bucket_capacity` | 5 | no | 30 |
| `refill_seconds` | 30 | no | 10 (floor) |

The code ceilings apply regardless of the config or provider. You cannot raise these limits by editing the config; the server will refuse to start.

### One stimulus at a time

Both channels share one fire gate, so a shock and a vibration never overlap, and neither do two shocks. The gate closes when a call is granted. It stays closed while the device call is in flight, then for a settle window measured from when the call returned:

- after a shock: the burst's `duration_s` plus one second. Shock backends answer as soon as they accept the command, so the burst is still running when the call returns.
- after a vibration: one second. The Intiface provider only returns once its explicit stop has landed.
- after any failed call, on either channel: the full `duration_s` plus one second. A timeout or a lost response does not prove that nothing fired.

A call that arrives while the gate is closed is refused with `error: "busy: …"` and a `next_available_at` timestamp. It spends no token and is logged like any other refusal. Nothing is queued, so a call cannot fire after your client has stopped waiting for it. `rlaif_info` reports when the gate reopens as `rate_limit.busy_until`, or `null` if it is open.

The one-second margin covers the delay between a backend accepting a command and the device starting. It narrows the overlap window, but cannot rule out overlap if a backend holds commands for longer.

### Disconnect watchdog (positive channel)

The positive provider promises that the device stops at the end of `duration_s` even if the controller process dies. The contract has three layers:

1. **Explicit stop after duration_s.** If the explicit stop call fails the safety layer logs a `RewardWatchdogError`. 
2. **atexit handler.** When the Python process exits cleanly, rlaif sends `stop_all_devices()` and disconnects the WS. 
3. **Signal handler (SIGINT, SIGTERM).** Same emergency stop.

A hard kill (SIGKILL, kernel panic, power loss) bypasses all three layers. This may cause the positive feedback device to run until its battery dies. If the device you own has a long battery life and you want a hard ceiling, please consider a physical kill switch.

Please keep the caps at what you are comfortable with.

---

## CLI

```
rlaif init                        interactive first-run setup (writes config, runs doctor, multi-select auto-install)
rlaif doctor                      read-only health check (config, both channels, provider-agnostic)
rlaif snippet X                   emit MCP client config snippet (X is one of the 16 clients)
rlaif install [X]                 auto-write rlaif into a supported MCP client config (X is one of the 14 auto-install clients; omit X to detect and install everywhere)
rlaif uninstall [X]               remove rlaif from one of the same 14 supported configs (omit X to detect and remove everywhere)
rlaif serve                       start the MCP server over stdio
rlaif log                         tail the on-disk ops log (default: last 10 entries, --tail N to change)
rlaif log --stats                 print rolling histograms (intensity buckets, refusal reasons, hourly volume) across both channels
rlaif dry-run                     exercise every tool against mock providers; nonzero on violation
rlaif live-smoke --channel ...    fire one real minimum-intensity call (interactive confirm)
                                  --channel negative (default) or --channel positive
```

`python -m rlaif <subcommand>` does the same thing.

---

## Safety

There is no built in stop button, so an agent may fire either channel too much. If you need to stop it immediately:

1. **Ctrl-C or kill the MCP server process.** This triggers the emergency-stop paths.
2. **Pause the device** at the device provider's dashboard.
3. **Unplug or power off the device.**

Restarting the server clears the cooldowns and the fire gate on both channels. Please do not deliberately restart the server to skip the cooldown.

---

## Troubleshooting

### Negative channel

- **`negative.device.online == false`** Please check these potential issues. PiShock: (a) your `shocker_id` (the share code) is correct, (b) the share code is claimed by the account that owns your `api_token`, (c) the device is online at pishock.com. OpenShock: (a) your `api_token` is valid and not expired, (b) the `shocker_id` matches a device your token has permission for. If that is configured correctly, (d) ensure the device is not paused on the provider dashboard. `rlaif doctor` will display these issues if they are present.

- **401 or 403 from PiShock.** Your `api_token` is wrong or revoked. If `rlaif doctor` says the token belongs to a different PiShock user than `negative.pishock.username`, you pasted a key from another account.

- **PiShock refuses with "hub is not on V3 firmware".** PiShock's v3 API only drives hubs on V3 firmware. Please update the hub from pishock.com.

- **PiShock refuses with "share does not allow shocking", "share is locked", or "exceeds the share's limit".** The share code's own settings on pishock.com are tighter than rlaif's caps. Please adjust the share, or lower rlaif's caps to fit inside it.

- **401 or 403 from OpenShock.** Your `api_token` is wrong, expired, or missing the `Shockers.Use` permission.

- **404 from OpenShock on shock.** The `shocker_id` is unknown or not shared with your token. Please double-check the UUID.

- **`negative` refuses with `device_offline`.** The backend could not be reached or did not answer in time. Info calls can succeed when the physical device isn't online, because `.info()` returns server-side metadata. Please wait for the device to reconnect, or pause and unpause it at the provider.

- **Upstream rate limit (separate from rlaif's bucket).** Both providers rate-limit API traffic on their side. If you see an error mentioning throttling, that is from upstream and rlaif can do nothing about it.

### Positive channel

- **`positive.device.online == false`.** Please check that Intiface Central is running and that the device appears in the devices panel. The most common cause is the gateway not being open: rlaif tries to connect to `ws://localhost:12345` by default, and a connection refused there means the provider is not listening.

- **`positive` refuses with `auth_error`.** The Intiface server rejected the WS handshake. The gateway version may be too old; please update Intiface Central to the latest release.

- **`positive` returns a `watchdog` error.** The explicit stop after `duration_s` did not deliver to the gateway. The device may still be running until its next ping cycle stops it, or until you ctrl-c the server. Please check the WS link to Intiface.

- **The device continues to fire after I ctrl-c the server.** The signal handler is best-effort; the emergency stop call may not complete. Please pause or close Intiface Central, or deactivate the device.

### General

- **A fire call refuses with `busy: …`.** Another stimulus is running or settling (see [One stimulus at a time](#one-stimulus-at-a-time)). Please retry after the `next_available_at` in the error, or check `rate_limit.busy_until` in `rlaif_info`.

- **Safety gate fires at startup.** If `[negative.safety] max_intensity > 25` or `bucket_capacity > 3` and `i_understand_and_consent = false`, the server refuses to start. This is intentional. Please reduce the caps or enable the consent flag.

- **Config path on Windows.** The default is `%USERPROFILE%\.config\rlaif\config.toml`, not `%APPDATA%`. If you set `XDG_CONFIG_HOME`, rlaif uses that instead. `rlaif init` writes to whichever path resolves, so please run it rather than creating the file by hand.

- **Config file permissions.** `rlaif init` writes the config with mode `0600` so other users on the same machine cannot read your secrets. 

- **Env vars override the config file.** `RLAIF_PISHOCK_*`, `RLAIF_OPENSHOCK_*`, and `RLAIF_INTIFACE_BASE_URL` take precedence over the values in the matching channel block. If the credentials in the config file look right but rlaif seems to be using different ones, please check whether one of these env vars is set in your shell or in your MCP client's launch environment.

---

## Architecture

```
src/rlaif/
  safety.py             # pure Python core: caps, token bucket, fire gate, ops log, channel specs
  config.py             # TOML loader, env overrides, [negative]/[positive] schema
  providers/            # negative-channel backends
    base.py             # Provider ABC + ProviderError taxonomy + DeviceInfo
    pishock.py          # PiShockProvider (httpx2; PiShock v3 REST API)
    openshock.py        # OpenShockProvider (httpx2; cloud or self-hosted)
    mock.py             # MockProvider for tests + dry-run
  rewards/              # positive-channel backends, in a parallel namespace
    base.py             # RewardProvider ABC + RewardProviderError taxonomy
    intiface.py         # IntifaceProvider (buttplug.io over WebSocket)
    mock.py             # MockRewardProvider for tests + dry-run
  server.py             # MCPServer wiring (thin); registers up to four tools
  cli.py                # `rlaif` entry point and subcommand dispatcher
  init.py               # `rlaif init` (per-channel wizard)
  doctor.py             # `rlaif doctor` (probes both channels)
  snippet.py            # `rlaif snippet` and per-client snippet builders
  installer.py          # `rlaif install` / `rlaif uninstall`; format adapters
  _clients.py           # single registry of every supported MCP client
  log.py                # `rlaif log` (tail and --stats)
  dry_run.py            # `rlaif dry-run` (mocks both channels)
  live_smoke.py         # `rlaif live-smoke --channel {negative,positive}`
  _util.py              # tiny shared helpers
```

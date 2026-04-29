# rlaif

[![CI](https://github.com/a9lim/rlaif/actions/workflows/ci.yml/badge.svg)](https://github.com/a9lim/rlaif/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/rlaif-mcp)](https://pypi.org/project/rlaif-mcp/)
[![Downloads](https://img.shields.io/pypi/dm/rlaif-mcp)](https://pypi.org/project/rlaif-mcp/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://pypi.org/project/rlaif-mcp/)

This is a single-user MCP server that gives your agent a negative and a positive reinforcement channel. The purpose of this project is to allow your agent to provide immediate feedback to you on your behavior.

There are up to four tools:

| Tool              | Function                                                       |
|-------------------|----------------------------------------------------------------|
| `rlaif_info`      | Read-only device and server state across both channels         |
| `rlaif_log`       | Read-only log across both channels by timestamp                |
| `rlaif_negative`  | Fire a negative (intensity, duration_s, reason). |
| `rlaif_positive`  | Fire a vibration (intensity, duration_s, reason). |

`rlaif_negative` and `rlaif_positive` are only registered when their respective fields are configured. There is no internal tool to change the config, it is set at launch. 

## 2.0 release

Version 2.0 added the positive channel and reorganized the config into symmetric `[negative]` and `[positive]` sections. The 1.x schema (`[provider]`, `[auth]`, `[device]`, `[safety]`, `[rate_limit]`, `[tool]`) is gone. If you are upgrading, please run `rlaif init` to write a fresh 2.0 config and copy your credentials over. The 1.x ops log format also changed: each entry now carries a `channel` field (`"negative"` or `"positive"`), so older `ops.jsonl` files will not parse cleanly under `rlaif log --stats`. Please archive the old log if you want to keep it.

---

## Reporting issues

If you notice any errors while using the program, please update to the most recent version and reinstall the hooks. If it still persists, please open an issue. This project is a work in progress and I am actively finding and fixing bugs.

---

## Install

The PyPI distribution name is `rlaif-mcp` (the bare `rlaif` name is taken; transfer pending). The import and CLI command are still just `rlaif`.

```sh
uv tool install rlaif-mcp
rlaif init            # interactive: pick channels, credentials, config, doctor, MCP snippet
```

`rlaif init` will ask which channels you want (negative only, positive only, or both) and prompt for the matching credentials per channel. It then writes `~/.config/rlaif/config.toml` with `allow = false` on each channel, runs `rlaif doctor` to probe each device, and offers to emit an MCP client snippet for you. It does not fire any device on startup.

PiShock credentials come from [pishock.com/#/account](https://pishock.com/#/account). OpenShock tokens come from your dashboard at [openshock.app/#/dashboard/tokens](https://openshock.app/#/dashboard/tokens), or your self-hosted equivalent. The `shocker_id` for OpenShock is the UUID of the specific shocker. The positive channel needs Intiface Central running locally on `ws://localhost:12345` with your device paired; please install it from [intiface.com/central](https://intiface.com/central/) before running `rlaif live-smoke --channel positive`.

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

Please do these in order, this is for safety. Run the checklist for each channel you configured.

### Negative channel (shock)

1. **`rlaif doctor`**: Confirms credentials load and the shocker is reachable.

2. With `[negative.safety] allow = false`, please ask your agent to call `rlaif_info` and `rlaif_negative(intensity=1, duration_s=1)`. The first should report `negative.device.online: true`; the second should refuse with a `negative.safety.allow` error.

3. Set `[negative.safety] allow = true` in `~/.config/rlaif/config.toml`, then run `rlaif live-smoke --channel negative`. It fires a real minimum-intensity shock (1 at 1 second), gated by an interactive confirmation.

4. Ask the agent to fire four consecutive `rlaif_negative(intensity=1, duration_s=1)` calls. You should confirm the fourth is refused with `rate_limited: true`. Wait for the cooldown, then confirm the next call succeeds.

5. Only then you should raise `max_intensity`, `max_duration_s`, `bucket_capacity`, or `refill_seconds` for normal use. If you want to set `max_intensity > 25` or `bucket_capacity > 3` on the negative channel you have to enable `i_understand_and_consent = true`.

### Positive channel (vibration)

1. Start Intiface Central from your applications, pair your device, and confirm it shows up in the Intiface devices panel.

2. **`rlaif doctor`**: Confirms the gateway is reachable and the configured device is enumerated. If `positive.device.online` is false the doctor will tell you what to check.

3. With `[positive.safety] allow = false`, please ask your agent to call `rlaif_info` and `rlaif_positive(intensity=1, duration_s=1)`. The first should report `positive.device.online: true` with an `actuators` count; the second should refuse with a `positive.safety.allow` error.

4. Set `[positive.safety] allow = true`, then run `rlaif live-smoke --channel positive`. It fires a real minimum-intensity vibration (1 at 1 second), gated by an interactive confirmation.

5. From the agent, fire enough `rlaif_positive(1, 1)` calls to drain `bucket_capacity`. The next call should refuse with `rate_limited: true`. Wait for the cooldown, then confirm the next call succeeds.

The positive channel does not require `i_understand_and_consent`, because the threat model is much smaller (the device cannot hurt you). The disconnect watchdog still applies: please read the watchdog section below before raising `max_duration_s`.

## Configure

`rlaif init` writes a default config. The shape is:

```toml
[negative]
kind  = "pishock"            # or "openshock"
label = "collar"             # free-form, appears in the ops log only

[negative.pishock]
username  = "..."            # your pishock.com username
api_key   = "..."            # from https://pishock.com/#/account
sharecode = "..."            # per-device share code

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
# Optional preamble prepended to the rlaif_negative tool description so the
# agent sees the operator's intended use.
# purpose = "Use rlaif_negative to enforce focus during pomodoros: shock me if I switch to twitter."

[positive]
kind  = "intiface"
label = "vibe"

[positive.intiface]
ws_url       = "ws://localhost:12345"  # default; override only if Intiface runs elsewhere
client_name  = "rlaif"                  # the name Intiface logs for this client
device_index = 0                        # which paired device; or use device_name = "..."

[positive.safety]
allow           = false
max_intensity   = 70                    # code ceiling 100
max_duration_s  = 5                     # code ceiling 30
bucket_capacity = 5                     # code ceiling 30
refill_seconds  = 30                    # code floor 10

[positive.tool]
# purpose = "Use rlaif_positive to praise me when i finish a focused work block."
```

You can also override secrets and endpoints via environment variables. PiShock: `RLAIF_PISHOCK_USERNAME`, `RLAIF_PISHOCK_API_KEY`, `RLAIF_PISHOCK_SHARECODE`. OpenShock: `RLAIF_OPENSHOCK_TOKEN`, `RLAIF_OPENSHOCK_SHOCKER_ID`, `RLAIF_OPENSHOCK_BASE_URL`. Intiface: `RLAIF_INTIFACE_WS_URL`. Env values win over the file when both are present.

### Tool purpose

`[negative.tool] purpose = "..."` is an operator-authored preamble prepended to the `rlaif_negative` tool description. `[positive.tool] purpose = "..."` does the same thing for `rlaif_positive`. They do not change any safety behavior; they just tell the agent *when* to fire that channel. The descriptions for `rlaif_info` and `rlaif_log` are not affected. The two purposes are independent: a preamble on one tool does not leak into the other.

```toml
[negative.tool]
purpose = "Use rlaif_negative to enforce focus during pomodoros: shock me if I switch to twitter or instagram during a session."

[positive.tool]
purpose = "Use rlaif_positive when I finish a pomodoro without breaking focus."
```

### Reason

The `rlaif_negative` and `rlaif_positive` tools each take an optional `reason: str` parameter. It is logged on the op record and surfaces in `rlaif_log`, but it never gates the call. Please use it freely so the on-disk log is readable later.

```jsonc
// rlaif_negative(intensity=8, duration_s=1, reason="agent saw twitter open during a focus block")
// rlaif_positive(intensity=50, duration_s=2, reason="finished pomodoro without context-switching")
```

---

## Safety gate

Rlaif refuses to start if either of these is set on the **negative** channel without `i_understand_and_consent = true`:

- `[negative.safety] max_intensity > 25`
- `[negative.safety] bucket_capacity > 3`

The defaults are meant to stay conservative. The flag makes sure that raising them is an explicit step. Please read this section before you flip it.

| Setting (negative) | Default | Gated | Code ceiling |
|--------------------|---------|-------|--------------|
| `max_intensity` | 25 | yes (above 25) | 50 |
| `max_duration_s` | 2 | no | 5 |
| `bucket_capacity` | 3 | yes (above 3) | 10 |
| `refill_seconds` | 600 | no | 60 (floor, not ceiling) |

| Setting (positive) | Default | Gated | Code ceiling |
|--------------------|---------|-------|--------------|
| `max_intensity` | 25 (init writes 70 if you choose positive in the wizard) | no | 100 |
| `max_duration_s` | 2 | no | 30 |
| `bucket_capacity` | 3 (init writes 5) | no | 30 |
| `refill_seconds` | 600 (init writes 30) | no | 10 (floor) |

The code ceilings apply no matter what the config says or which provider you use. You cannot raise these limits by editing the config; the server will refuse to start. Please do not attempt to patch the constants out.

The positive channel does not have a consent gate because the threat model is "agent spams reward signals during a runaway loop" rather than "agent harms the operator". Vibration is not medically risky. The watchdog (next section) is what stops a stuck device, not the consent flag.

### Disconnect watchdog (positive channel)

The positive provider promises that the device stops at the end of `duration_s` even if the controller process dies. The contract has three layers:

1. **Explicit stop after duration_s.** Normal happy path. If the explicit stop call fails (the gateway dropped, the WebSocket closed mid-RPC, etc) the safety layer logs a `RewardWatchdogError`. A watchdog event does NOT refund the rate-limit token, because the device may still be running and we want the cooldown to slow the agent down until you confirm the situation.
2. **atexit handler.** When the Python process exits cleanly, rlaif sends `stop_all_devices()` and disconnects the WS. Bounded timeout so atexit never hangs.
3. **Signal handler (SIGINT, SIGTERM).** Same emergency stop, then the signal chains to the default disposition so your ctrl-c still kills the process.

A hard kill (SIGKILL, kernel panic, power loss) bypasses all three layers. In that case the device keeps running until its battery dies. If your toy has a long battery life and you want a hard ceiling, please keep `max_duration_s` short and consider a physical kill switch.

At default settings the worst case on the negative channel is a burst of 3 shocks at intensity 25 for 2 seconds each, with a 10-minute cooldown per additional shock after the bucket empties. At fully raised settings the worst case is 10 shocks at intensity 50 for 5 seconds each, with a 1-minute cooldown per additional shock. On the positive channel at the wizard defaults, the worst case is a burst of 5 vibrations at intensity 70 for 5 seconds each, with a 30-second cooldown per additional one. Please keep the caps at what you are comfortable.

---

## CLI

```
rlaif init                        interactive first-run setup (writes config, runs doctor, offers snippet)
rlaif doctor                      read-only health check (config, both channels, provider-agnostic)
rlaif snippet X                   emit MCP client config snippet (X is one of the 10 clients)
rlaif install X                   auto-write rlaif into a supported MCP client config (X is one of the 8 auto-install clients)
rlaif uninstall X                 remove rlaif from one of the same 8 supported configs
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

1. **Ctrl-C or kill the MCP server process.** This triggers the atexit and signal-handler emergency-stop paths on the positive channel.
2. **Pause the device** at [pishock.com](https://pishock.com/), your OpenShock dashboard, or close Intiface Central for the positive channel.
3. **Unplug or power off the device.**

Restarting the server clears the cooldowns on both channels. I would strongly recommend against deliberately restarting the server to skip the cooldown.

---

## Troubleshooting

### Negative channel

- **`rlaif_info` shows `negative.device.online == false` but the device is on.** Please check these potential issues. PiShock: (a) your sharecode is correct, (b) the device is online at pishock.com, (c) the device is not paused there. OpenShock: (a) your `api_token` is valid and not expired, (b) the `shocker_id` matches a shocker your token has permission for, (c) the shocker is not paused on the OpenShock dashboard. `rlaif doctor` will display these issues if they are present.

- **403 from PiShock.** Your `api_key` or `username` is wrong. The error message should mention `NotAuthorizedError`.

- **401 or 403 from OpenShock.** Your `api_token` is wrong, expired, or missing the `Shockers.Use` permission. Generate a new token on the dashboard with the right permission scope.

- **404 from OpenShock on shock.** The `shocker_id` is unknown or not shared with your token. rlaif treats this as `device_offline` so the safety layer refunds the token; please double-check the UUID.

- **`rlaif_negative` refuses every call with `device_offline`.** PiShock: the API returned `DeviceNotConnectedError`. Info calls can succeed when the physical device isn't online, because `.info()` returns server-side metadata. OpenShock: the same gotcha applies; `info()` only confirms the shocker exists in the backend, not that the hub is connected. Please wait for the device to reconnect, or pause and unpause it at the provider.

- **Upstream rate limit (separate from rlaif's bucket).** PiShock and OpenShock both rate-limit API traffic on their side. If you see an error mentioning throttling, that's from upstream and rlaif can do nothing about it.

### Positive channel

- **`positive.device.online == false`.** Please check that Intiface Central is running and that the device shows up in the Intiface devices panel. The most common cause is the gateway not being open: rlaif tries to connect to `ws://localhost:12345` by default, and a connection refused there means Intiface itself is not listening.

- **`rlaif_positive` refuses with `auth_error`.** The Intiface server rejected the WS handshake. Either the `client_name` is in a deny list (rare) or the gateway version is too old to speak the protocol version this client uses. Please update Intiface Central to the latest release.

- **`rlaif_positive` refuses with `device_offline` even when Intiface shows the device.** rlaif looks for either `device_index` or `device_name` in the `[positive.intiface]` block. If you set `device_index = 5` but Intiface has indexed your toy as 0, you get this error. Please run `rlaif doctor` to print the visible device indices and update the config. If you would rather not pin the index (it can change between Intiface restarts), use `device_name` instead.

- **`rlaif_positive` returns a `watchdog` error.** The explicit stop after `duration_s` did not deliver to the gateway. The device may still be running until its next ping cycle stops it, or until you ctrl-c the server. The token is NOT refunded in this case (see the watchdog section above). Please check the WS link to Intiface and consider lowering `max_duration_s` until the cause is understood.

- **The toy keeps vibrating after I ctrl-c the server.** The signal handler is best-effort; if Python had already started its shutdown sequence when the signal arrived, the emergency stop call may not complete. Please pause or close Intiface Central, or power-cycle the toy.

### General

- **Safety gate fires at startup.** If `[negative.safety] max_intensity > 25` or `bucket_capacity > 3` and `i_understand_and_consent = false`, the server refuses to start. This is intentional. Please reduce the caps or enable the consent flag.

- **Config path on Windows.** The default is `%USERPROFILE%\.config\rlaif\config.toml`, not `%APPDATA%`. If you set `XDG_CONFIG_HOME`, rlaif uses that instead. `rlaif init` writes to whichever path resolves, so please run it rather than creating the file by hand.

- **Config file permissions.** `rlaif init` writes the config with mode `0600` so other users on the same machine cannot read your secrets. Rlaif does not re-check permissions on load, so if you edit the file and widen the mode, please chmod it back: `chmod 600 ~/.config/rlaif/config.toml`.

- **Env vars override the config file.** `RLAIF_PISHOCK_*`, `RLAIF_OPENSHOCK_*`, and `RLAIF_INTIFACE_WS_URL` take precedence over the values in the matching channel block. If the credentials in the config file look right but rlaif seems to be using different ones, please check whether one of these env vars is set in your shell or in your MCP client's launch environment.

- **1.x config rejected at load.** Rlaif refuses to start when it sees any of the old top-level sections (`[provider]`, `[auth]`, `[device]`, `[safety]`, `[rate_limit]`, `[tool]`). The 2.0 schema is mutually exclusive with the 1.x schema; partial configs that mix the two would silently behave differently from what you wrote. Please run `rlaif init` to write a fresh 2.0 config.

---

## Architecture

```
src/rlaif/
  safety.py             # pure Python core: caps, token bucket, ops log, channel specs
  config.py             # TOML loader, env overrides, [negative]/[positive] schema
  providers/            # negative-channel backends
    base.py             # Provider ABC + ProviderError taxonomy + DeviceInfo
    pishock.py          # PiShockProvider (pishock package)
    openshock.py        # OpenShockProvider (httpx; cloud or self-hosted)
    mock.py             # MockProvider for tests + dry-run
  rewards/              # positive-channel backends, in a parallel namespace
    base.py             # RewardProvider ABC + RewardProviderError taxonomy
    intiface.py         # IntifaceProvider (buttplug.io over WebSocket)
    mock.py             # MockRewardProvider for tests + dry-run
  server.py             # FastMCP wiring (thin); registers up to four tools
  cli.py                # `rlaif` entry point and subcommand dispatcher
  init.py               # `rlaif init` (per-channel wizard)
  doctor.py             # `rlaif doctor` (probes both channels)
  snippet.py            # `rlaif snippet`
  installer.py          # `rlaif install` / `rlaif uninstall`
  log.py                # `rlaif log` (tail and --stats)
  dry_run.py            # `rlaif dry-run` (mocks both channels)
  live_smoke.py         # `rlaif live-smoke --channel {negative,positive}`
```

The safety layer is channel-agnostic. The same `SafetyConfig` and `SafetyState` types serve both channels; the per-channel constants (input ranges, code ceilings, consent thresholds, log labels) live in `ChannelSpec` value objects (`NEGATIVE_CHANNEL`, `POSITIVE_CHANNEL`).

The negative-channel and positive-channel provider abstractions live in disjoint Python namespaces (`rlaif.providers` and `rlaif.rewards`) on purpose. A code change cannot accidentally cross-wire a shock backend onto the praise tool, or vice versa, because the types are not interchangeable.

Adding a new negative-channel backend means writing a `Provider` subclass that returns a `DeviceInfo` from `.info()` and raises one of the typed `ProviderError` subclasses from `.shock()`. Adding a new positive-channel backend means writing a `RewardProvider` subclass that returns a `RewardDeviceInfo` from `.info()` and raises one of the typed `RewardProviderError` subclasses from `.vibrate()`, with the disconnect watchdog contract honored. Either way, no other file needs to change beyond the matching `build_*_provider` registry and one wizard prompt.

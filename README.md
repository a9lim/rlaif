# rlaif

[![CI](https://github.com/a9lim/rlaif/actions/workflows/ci.yml/badge.svg)](https://github.com/a9lim/rlaif/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/rlaif)](https://pypi.org/project/rlaif/)
[![Downloads](https://img.shields.io/pypi/dm/rlaif)](https://pypi.org/project/rlaif/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://pypi.org/project/rlaif/)

This is a single-user MCP server that provides a shock tool.

There are three tools:

| Tool        | Function                         |
|-------------|----------------------------------|
| `rlaif_info`| Read-only device and server state|
| `rlaif_log` | Read-only log                    |
| `rlaif`     | Fire a shock                     |

There is no tool to change the config, it is set at launch. There is also no tool for beep or vibrate. The only purpose of this project is for your agent to be able to zap you.

---

## Install

```sh
uv tool install rlaif
rlaif init            # interactive: credentials, config, doctor, MCP snippet
```

`rlaif init` will ask you for your credentials; get them from [pishock.com/#/account](https://pishock.com/#/account). It then writes `~/.config/rlaif/config.toml` with `allow_shock = false`, runs `rlaif doctor` to probe the device, and offers to emit an MCP client snippet for you. It does not fire the device on startup.

From a source checkout:

```sh
git clone <repo> rlaif
cd rlaif
uv sync               # creates .venv, installs deps
uv run rlaif init     # same wizard, running from the checkout
```

## Connect MCP client

The CLI prints a snippet to copy for each supported client:

```sh
rlaif snippet claude-desktop   # JSON for ~/Library/.../claude_desktop_config.json
rlaif snippet claude-code      # JSON for ~/.claude.json or project .claude.json
rlaif snippet codex            # TOML for ~/.codex/config.toml
rlaif snippet hermes           # YAML for ~/.hermes/config.yaml
```

After `uv tool install rlaif` the snippet is a one-liner: `"command": "rlaif", "args": ["serve"]`. For dev mode, please pass `--dev-path /absolute/path/to/rlaif` to get a `uv run --directory …` variant.

## Before use

Please do these in order, this is for safety.

1. **`rlaif doctor`**: Confirms PiShock credentials load and the collar is reachable.

2. With `allow_shock = false`, please ask your agent to call `rlaif_info` and `rlaif(intensity=1, duration_s=1)`. The first one should report `device.online: true`; the second should refuse with an `allow_shock` error.

3. Set `allow_shock = true` in `~/.config/rlaif/config.toml`, then run `rlaif live-smoke`. It fires a real minimum-intensity shock (1 at 1 second) to the collar, gated by a confirmation.

4. Ask the agent to fire four consecutive `rlaif(intensity=1, duration_s=1)` calls. You should confirm the fourth is refused with `rate_limited: true`. Wait for the cooldown, then confirm the next call succeeds.

5. Only then you should raise `max_intensity`, `max_duration_s`, `bucket_capacity`, or `refill_seconds` for normal use. If you want to set `max_intensity > 25` or `bucket_capacity > 3` you have to enable `i_understand_and_consent = true`.

## Configure

`rlaif init` writes a default config:

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

You can also override the secrets via environment variables: `RLAIF_USERNAME`, `RLAIF_API_KEY`, and `RLAIF_SHARECODE`.

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

The code ceilings apply no matter what the config says. You cannot raise `max_intensity` above 50 or `bucket_capacity` above 10 by editing the config, and `refill_seconds` cannot go below 60; the server will refuse to start. Please do not attempt to patch these constants out.

At default settings the worst case is a burst of 3 shocks at intensity 25 for 2 seconds each, with a 10-minute cooldown per additional shock after the bucket empties. At fully raised settings with consent the worst case is 10 shocks at intensity 50 for 5 seconds each, with a 1-minute cooldown per additional shock. Please keep the caps at what you are genuinely comfortable with; you can always lower them later.

---

## CLI

```
rlaif init         interactive first-run setup (writes config, runs doctor, offers snippet)
rlaif doctor       read-only health check (config and device probe)
rlaif snippet X    emit MCP client config snippet (X is claude-desktop, claude-code, codex, or hermes)
rlaif serve        start the MCP server over stdio
rlaif log          tail the on-disk ops log (default: last 10 entries, --tail N to change)
rlaif dry-run      exercise every tool against a mock device; nonzero on violation
rlaif live-smoke   fire one real minimum-intensity shock (interactive confirm)
```

`python -m rlaif <subcommand>` does the same thing.

---

## Safety

There is no built in stop button, so an agent may shock you too much. If you need to stop it immediately:

1. **Ctrl-C or kill the MCP server process.** 
2. **Pause the device on [pishock.com](https://pishock.com/).** 
3. **Unplug the device.** 

Restarting the server clears the cooldowns. I would strongly recommend against deliberately restarting the server to skip the cooldown.

---

## Troubleshooting

- **`rlaif_info.device.online == false` but the device is on.** Please check these potential issues: (a) your share code is correct, (b) the device is online at pishock.com, and (c) the device is not paused there. `rlaif doctor` will display these issues if they are present.

- **403 from upstream.** Your `api_key` or `username` is wrong. The error message should mention `NotAuthorizedError`.

- **`rlaif` refuses every call with `device_offline`.** The PiShock API returned `DeviceNotConnectedError`. Info calls can succeed when the physical device isn't online, because `.info()` returns server-side metadata. Please wait for the device to reconnect, or pause and unpause it on pishock.com.

- **Upstream rate limit (separate from rlaif's bucket).** PiShock itself rate-limits API traffic. If you see `UnknownError` with a message about throttling, that's from PiShock and rlaif can do nothing about it.

- **Safety gate fires at startup.** If `max_intensity > 25` or `bucket_capacity > 3` and `i_understand_and_consent = false`, the server refuses to start. This is intentional. Please reduce the caps or enable the consent flag.

- **Config path on Windows.** The default is `%USERPROFILE%\.config\rlaif\config.toml`, not `%APPDATA%`. If you set `XDG_CONFIG_HOME`, rlaif uses that instead. `rlaif init` writes to whichever path resolves, so please run it rather than creating the file by hand.

- **Config file permissions.** `rlaif init` writes the config with mode `0600` so other users on the same machine cannot read your PiShock API key. Rlaif does not re-check permissions on load, so if you edit the file and widen the mode, please chmod it back: `chmod 600 ~/.config/rlaif/config.toml`.

- **Env vars override the config file.** `RLAIF_USERNAME`, `RLAIF_API_KEY`, and `RLAIF_SHARECODE` take precedence over the values in `[auth]`. If the credentials in the config file look right but rlaif seems to be using different ones, please check whether one of these env vars is set in your shell or in your MCP client's launch environment.

---

## Architecture

```
src/rlaif/
  safety.py     # pure Python core: caps, token bucket, ops log, safety gate
  config.py     # TOML loader, env overrides, validation
  server.py     # FastMCP wiring (thin) + on-disk ops log sink
  cli.py        # `rlaif` entry point and subcommand dispatcher
  init.py       # `rlaif init`
  doctor.py     # `rlaif doctor`
  snippet.py    # `rlaif snippet`
  log.py        # `rlaif log`
  dry_run.py    # `rlaif dry-run`
  live_smoke.py # `rlaif live-smoke`
```

# rlaif

A single-user MCP server exposing a rate-limited PiShock shock tool to
Claude Code, Claude Desktop, Codex, and Hermes Agent.

Three tools, in strict order of capability:

| Tool        | What it does                                              |
|-------------|-----------------------------------------------------------|
| `rlaif_info`| Read-only device + server state                           |
| `rlaif_log` | Read-only in-memory op log                                |
| `rlaif`     | Fire a shock. Clamped, rate-limited, refusable            |

There is **no** lockout/unlock tool, **no** config-mutation tool, **no**
beep/vibrate. Hard stops are out-of-band — see [Hard stops](#hard-stops).

---

## Dependencies

Pinned to `pishock==1.2.1`. Note that the **PyPI name is `pishock`**, not
`python-pishock` as the `python-pishock` package name on readthedocs may
suggest. `mcp>=1.2.0` for the server SDK.

## Install

```sh
git clone <repo> rlaif
cd rlaif
uv sync                 # creates .venv, installs deps
uv run pytest           # 87 tests should pass
uv run python scripts/dry_run.py   # exits 0 if safety invariants hold
```

## Configure

Create `~/.config/rlaif/config.toml` (respects `$XDG_CONFIG_HOME`):

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

**Env overrides** (for secrets you don't want on disk):
`RLAIF_USERNAME`, `RLAIF_API_KEY`, `RLAIF_SHARECODE`.

### Consent gate

The server **refuses to start** if any of these are true without
`i_understand_and_consent = true`:

- `max_intensity > 25`
- `bucket_capacity > 3`

The code ceilings still apply regardless — `max_intensity` cannot exceed
50, `max_duration_s` cannot exceed 5, `bucket_capacity` cannot exceed 10,
and `refill_seconds` cannot fall below 60.

---

## Client snippets

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "rlaif": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/absolute/path/to/rlaif",
        "python", "-m", "rlaif.server"
      ]
    }
  }
}
```

### Claude Code

`.claude.json` (project) or `~/.claude.json` (user):

```json
{
  "mcpServers": {
    "rlaif": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/absolute/path/to/rlaif",
        "python", "-m", "rlaif.server"
      ]
    }
  }
}
```

Or via CLI:

```sh
claude mcp add rlaif -- uv run --directory /absolute/path/to/rlaif python -m rlaif.server
```

### Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.rlaif]
command = "uv"
args    = ["run", "--directory", "/absolute/path/to/rlaif", "python", "-m", "rlaif.server"]
```

### Hermes Agent (Nous Research)

`~/.hermes/config.yaml`:

```yaml
mcp_servers:
  rlaif:
    command: "uv"
    args: ["run", "--directory", "/path/to/rlaif", "python", "-m", "rlaif.server"]
    tools:
      # explicitly whitelist; rlaif (the shock tool) is only useful with allow_shock=true
      include: [rlaif_info, rlaif_log, rlaif]
      prompts: false
      resources: false
```

---

## Before first use

Do these in order. Skipping is circumventing your own safety layer.

1. **With `allow_shock = false`,** start the server. Ask the agent to
   call `rlaif_info`. Confirm it reports `device.online: true`. Ask it
   to call `rlaif(intensity=1, duration_s=1)`. Confirm it refuses with
   an error mentioning `allow_shock`.

2. **Flip `allow_shock = true`** in your config. Restart the server.
   Fire `rlaif(intensity=1, duration_s=1)` once. This is a real
   (minimum) shock — the cheapest possible end-to-end wiring check.
   `scripts/live_smoke.py` does exactly this with a confirmation
   prompt.

3. **Drain the bucket.** Fire four more `rlaif(intensity=1, duration_s=1)`
   calls back-to-back. Confirm the 4th is refused with
   `rate_limited: true` and a `next_available_at` timestamp. Wait the
   cooldown (`refill_seconds`). Confirm the next call succeeds.

4. **Inspect the log.** Call `rlaif_log(limit=10)`. Verify each entry
   has matching `requested` and `actual` (they should, since 1/1 is
   under every cap).

5. **Only then** adjust `max_intensity`, `max_duration_s`,
   `bucket_capacity`, or `refill_seconds` for normal use. If you need
   to raise `max_intensity` above 25 or `bucket_capacity` above 3, set
   `i_understand_and_consent = true`.

---

## Hard stops

There is no in-band panic button. The tool intentionally does not expose
a "lockout" or "unlock" — an agent with write access to that tool could
neutralize the safety layer on its own. If you need to stop *right now*:

1. **Ctrl-C / kill the MCP server process.** A dead process cannot fire
   the device.
2. **Pause the device on [pishock.com](https://pishock.com/).** Stops
   the hardware from accepting any shock command.
3. **Unplug the device.** The ultimate panic button.

State is **in-memory only**. Restarting the server clears the token
bucket (refills to full) and the ops log. A crash mid-cooldown is safe
— a dead process can't fire the device. Deliberately restarting to
refill the bucket is circumventing your own safety layer; don't do it.

---

## Troubleshooting

- **`rlaif_info.device.online == false` but the device is plugged in.**
  Check in order: (a) your share code is correct; (b) the device
  reports online at pishock.com; (c) the device is not paused there.

- **403 from upstream.** Your `api_key` or `username` is wrong. The
  error message from the server will mention `NotAuthorizedError`.

- **`rlaif` refuses every call with `device_offline`.** The PiShock API
  returned `DeviceNotConnectedError` at shock time. Info calls can
  succeed even when the physical device isn't online, because
  `.info()` returns server-side metadata. Wait for the device to
  reconnect, or pause/unpause from pishock.com.

- **Upstream rate limit (separate from rlaif's bucket).** PiShock
  itself rate-limits API traffic. If you see `UnknownError` with a
  message about throttling, that's upstream, and rlaif can do nothing
  about it beyond surfacing it in the ops log.

- **Consent gate fires at startup.** If `max_intensity > 25` or
  `bucket_capacity > 3` and `i_understand_and_consent = false`, the
  server refuses to start. This is intentional. Reduce the caps or
  set the consent flag.

---

## Architecture

```
src/rlaif/
  safety.py   # pure Python core — caps, token bucket, ops log, consent gate
  config.py   # TOML loader, env overrides, validation
  server.py   # FastMCP wiring (thin)
```

- `safety.py` has zero MCP imports. You can unit-test it standalone.
- `tests/test_safety.py` reads as the safety spec — an auditor can read
  it end-to-end and know what the server will and won't do.
- Tool description strings are module-level constants
  (`RLAIF_DESCRIPTION`, etc.); `tests/test_server.py` asserts they match
  the build spec byte-for-byte.

## Scripts

- `scripts/dry_run.py` — exercises every tool against a mock device,
  exits nonzero if any safety invariant is violated. Run after any
  nontrivial change to `safety.py`.
- `scripts/live_smoke.py` — fires one real `rlaif(intensity=1,
  duration_s=1)` against the actual device, gated by an interactive
  confirmation prompt.

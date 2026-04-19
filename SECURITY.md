# Security and safety

rlaif exposes physical hardware to an LLM. Bugs that let an agent exceed the configured caps, fire faster than `refill_seconds` allows, bypass the consent gate, or otherwise cause the device to fire when it shouldn't are treated as security-critical, even though they don't fit the classical infosec mold.

## Reporting

Email: **a9lim@protonmail.com**

Please include a minimal reproduction: a config excerpt (with secrets redacted) and the shortest tool call sequence that triggers the bypass. Please do **not** open a public issue for safety-bypass reports.

## In scope

- Bypass of any check in `src/rlaif/safety.py`: clamping, consent gate, `allow_shock`, or the token bucket.
- Any path where `handle_rlaif` causes a device fire without a successful `authorize` having granted a token.
- Ops-log entries that misrepresent what actually fired (silent drops, entries where `requested` and `actual` don't match, or missing refusal entries).
- Credential exfiltration via any of the three MCP tools.
- Config-load paths that accept values outside the code ceilings (`INTENSITY_CODE_CEILING`, `DURATION_CODE_CEILING_S`, `BUCKET_CAPACITY_CODE_CEILING`, or `REFILL_SECONDS_CODE_FLOOR`).

## Out of scope

- Upstream PiShock API behavior. Please report those to PiShock directly.
- Physical attacks on the device hardware.
- Attacks requiring local shell or filesystem access. That attacker already owns your user account and can do worse than fire a shock.
- Denial-of-service (restarting the server refills the bucket; this is documented as a feature, see the README "Hard stops" section).

## Supported versions

I only support the current `main` branch. There is no published release yet.

# Security and safety

rlaif exposes a shock collar to an agent. Bugs that let an agent exceed the configured caps, bypass the safety gate, or otherwise cause the device to fire when it shouldn't are treated as critical, even though they aren't traditional security issues.

## Reporting

Email: **mx@a9l.im**

Please include a description, steps to reproduce, your config (with secrets removed) and the shortest tool call sequence that causes the issue. I'll respond within a few days and aim to have a fix as soon as possible. 

## In scope

- Bypass of any check in `src/rlaif/safety.py`: value clamping, safety gate, `allow_shock`, or rate limits.
- Any path where `handle_rlaif` causes it to fire without a successful `authorize` having granted a token.
- Inaccurate log entries (silent drops, entries where `requested` and `actual` don't match, or missing refusal entries).
- Credentials leaking via any of the MCP tools.
- Config paths that accept values outside the hardcoded ceilings (`INTENSITY_CODE_CEILING`, `DURATION_CODE_CEILING_S`, `BUCKET_CAPACITY_CODE_CEILING`, or `REFILL_SECONDS_CODE_FLOOR`).

## Out of scope

- PiShock API issues. Please report those to PiShock directly.
- Physical attacks on the device hardware.
- Attacks requiring local device access.
- Denial-of-service.

## Supported versions

I only support the current `main` branch. There is no published release yet.

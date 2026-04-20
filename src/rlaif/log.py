"""`rlaif log` — tail the on-disk ops log.

The MCP server appends one JSON record per op to
``$XDG_STATE_HOME/rlaif/ops.jsonl`` (or ``~/.local/state/rlaif/ops.jsonl``).
This subcommand prints the most recent ``--tail N`` entries without needing
an MCP round-trip, so an operator can see what the agent has been up to
without spinning up a client.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from rlaif.config import default_log_path


def _pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def run(*, tail: int = 10, log_path: Path | None = None, raw: bool = False) -> int:
    path = log_path if log_path is not None else default_log_path()

    if not path.exists():
        print(f"no ops log at {path}", file=sys.stderr)
        print(
            "  (the server writes one line per op; start the server and fire "
            "something first)",
            file=sys.stderr,
        )
        return 0

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return 2

    lines = [line for line in lines if line.strip()]
    if tail > 0:
        lines = lines[-tail:]

    for line in lines:
        if raw:
            print(line)
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            print(line)
            continue
        print(_pretty(entry))

    return 0

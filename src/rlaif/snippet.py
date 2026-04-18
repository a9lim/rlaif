"""Emit MCP client config snippets for rlaif.

Default: assumes ``rlaif`` is on PATH (e.g. after ``uv tool install rlaif``)
and emits ``command: "rlaif", args: ["serve"]``.

``--dev-path /path/to/rlaif`` emits an ``uv run --directory`` variant for
running directly from a source checkout.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Callable


def _command_and_args(dev_path: str | None) -> tuple[str, list[str]]:
    if dev_path is not None:
        abs_path = str(Path(dev_path).expanduser().resolve())
        return "uv", ["run", "--directory", abs_path, "python", "-m", "rlaif", "serve"]
    return "rlaif", ["serve"]


def _json_mcp_servers(command: str, args: list[str]) -> str:
    return json.dumps(
        {"mcpServers": {"rlaif": {"command": command, "args": args}}},
        indent=2,
    )


def _codex(command: str, args: list[str]) -> str:
    args_repr = "[" + ", ".join(json.dumps(a) for a in args) + "]"
    return (
        "[mcp_servers.rlaif]\n"
        f'command = "{command}"\n'
        f"args    = {args_repr}\n"
    )


def _hermes(command: str, args: list[str]) -> str:
    args_yaml = "[" + ", ".join(json.dumps(a) for a in args) + "]"
    return (
        "mcp_servers:\n"
        "  rlaif:\n"
        f'    command: "{command}"\n'
        f"    args: {args_yaml}\n"
        "    tools:\n"
        "      include: [rlaif_info, rlaif_log, rlaif]\n"
        "      prompts: false\n"
        "      resources: false\n"
    )


Builder = Callable[[str, list[str]], str]

BUILDERS: dict[str, tuple[str, Builder]] = {
    "claude-desktop": (
        "~/Library/Application Support/Claude/claude_desktop_config.json",
        _json_mcp_servers,
    ),
    "claude-code": (
        "~/.claude.json (user) or .claude.json (project)",
        _json_mcp_servers,
    ),
    "codex": ("~/.codex/config.toml", _codex),
    "hermes": ("~/.hermes/config.yaml", _hermes),
}

CLIENTS = tuple(BUILDERS.keys())


def run(*, client: str, dev_path: str | None = None) -> int:
    if client not in BUILDERS:
        print(f"unknown client: {client}", file=sys.stderr)
        return 2
    location, builder = BUILDERS[client]
    command, args = _command_and_args(dev_path)

    if dev_path is None and shutil.which("rlaif") is None:
        print(
            "# note: `rlaif` was not found on PATH. install with "
            "`uv tool install rlaif`, or re-run with --dev-path /path/to/rlaif.",
            file=sys.stderr,
        )

    print(f"# paste into {location}\n")
    print(builder(command, args))
    return 0

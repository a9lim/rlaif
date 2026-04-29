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
from collections.abc import Callable
from pathlib import Path


def command_and_args(dev_path: str | None) -> tuple[str, list[str]]:
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


def _opencode(command: str, args: list[str]) -> str:
    # opencode's schema diverges from the rest:
    #   - top-level key is `mcp`, not `mcpServers`
    #   - each entry needs `type: "local"` for stdio
    #   - `command` is a single array (command+args merged)
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "rlaif": {
                    "type": "local",
                    "command": [command, *args],
                    "enabled": True,
                }
            },
        },
        indent=2,
    )


def _vscode(command: str, args: list[str]) -> str:
    # VS Code uses `servers` (not `mcpServers`) and requires `type: "stdio"`.
    return json.dumps(
        {
            "servers": {
                "rlaif": {
                    "type": "stdio",
                    "command": command,
                    "args": args,
                }
            }
        },
        indent=2,
    )


def _zed(command: str, args: list[str]) -> str:
    # Zed stores MCP servers under `context_servers` inside the multi-purpose
    # settings.json. Output is a fragment to merge into existing settings.
    return json.dumps(
        {
            "context_servers": {
                "rlaif": {
                    "source": "custom",
                    "command": command,
                    "args": args,
                    "env": {},
                }
            }
        },
        indent=2,
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
    "antigravity": (
        "~/.gemini/antigravity/mcp_config.json",
        _json_mcp_servers,
    ),
    "opencode": (
        "opencode.json (project) or ~/.config/opencode/opencode.json (global)",
        _opencode,
    ),
    "cursor": (
        "~/.cursor/mcp.json (user) or .cursor/mcp.json (project)",
        _json_mcp_servers,
    ),
    "windsurf": (
        "~/.codeium/windsurf/mcp_config.json",
        _json_mcp_servers,
    ),
    "vscode": (
        ".vscode/mcp.json (workspace) or user mcp.json",
        _vscode,
    ),
    "zed": (
        "~/.config/zed/settings.json (merge fragment into top level)",
        _zed,
    ),
}

CLIENTS = tuple(BUILDERS.keys())


def run(*, client: str, dev_path: str | None = None) -> int:
    if client not in BUILDERS:
        print(f"unknown client: {client}", file=sys.stderr)
        return 2
    location, builder = BUILDERS[client]
    command, args = command_and_args(dev_path)

    if dev_path is None and shutil.which("rlaif") is None:
        print(
            "# note: `rlaif` was not found on PATH. install with "
            "`uv tool install rlaif`, or re-run with --dev-path /path/to/rlaif.",
            file=sys.stderr,
        )

    print(f"# paste into {location}\n")
    print(builder(command, args))
    return 0

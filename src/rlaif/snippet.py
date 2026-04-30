"""Emit MCP client config snippets for rlaif.

Default: assumes ``rlaif`` is on PATH (e.g. after ``uv tool install rlaif``)
and emits ``command: "rlaif", args: ["serve"]``.

``--dev-path /path/to/rlaif`` emits an ``uv run --directory`` variant for
running directly from a source checkout.

The per-client snippet shapes live here as ``_*`` builder functions; the
client list and the (location_hint, builder) pairing live in
``rlaif._clients`` so installer/snippet/init read from a single registry.
``run()`` defers its registry import to break the import cycle.
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


def json_mcp_servers_builder(command: str, args: list[str]) -> str:
    return json.dumps(
        {"mcpServers": {"rlaif": {"command": command, "args": args}}},
        indent=2,
    )


def codex_builder(command: str, args: list[str]) -> str:
    args_repr = "[" + ", ".join(json.dumps(a) for a in args) + "]"
    return f'[mcp_servers.rlaif]\ncommand = "{command}"\nargs    = {args_repr}\n'


def hermes_builder(command: str, args: list[str]) -> str:
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


def opencode_builder(command: str, args: list[str]) -> str:
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


def vscode_builder(command: str, args: list[str]) -> str:
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


def zed_builder(command: str, args: list[str]) -> str:
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


def run(*, client: str, dev_path: str | None = None) -> int:
    # Deferred import: rlaif._clients imports the builders defined above.
    # See rlaif/_clients.py for the rationale.
    from rlaif._clients import CLIENTS_REGISTRY

    record = CLIENTS_REGISTRY.get(client)
    if record is None:
        print(f"unknown client: {client}", file=sys.stderr)
        return 2
    command, args = command_and_args(dev_path)

    if dev_path is None and shutil.which("rlaif") is None:
        print(
            "# note: `rlaif` was not found on PATH. install with "
            "`uv tool install rlaif`, or re-run with --dev-path /path/to/rlaif.",
            file=sys.stderr,
        )

    print(f"# paste into {record.location_hint}\n")
    print(record.snippet_builder(command, args))
    return 0

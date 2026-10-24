"""Emit MCP client config snippets for rlaif.

Default: assumes ``rlaif`` is on PATH (e.g. after ``uv tool install rlaif``)
and emits ``command: "rlaif", args: ["serve"]``.

``--dev-path /path/to/rlaif`` emits an ``uv run --directory`` variant for
running directly from a source checkout.

The per-client snippet shapes live here as ``*_builder`` functions; the
client list and the (location_hint, builder) pairing live in
``rlaif._clients`` so installer/snippet/init read from a single registry.
``run()`` defers its registry import to break the import cycle.

Clients whose ``mcpServers`` entry carries extra keys (copilot, droid) define
them once as ``*_ENTRY_EXTRA`` here. Both the snippet builder and the
installer's ``JsonAdapter`` build the entry through :func:`stdio_entry`, so
what ``rlaif snippet`` prints and what ``rlaif install`` writes (and treats as
"already installed") cannot drift apart.
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

# Copilot CLI wants an explicit transport and a tool allowlist; ``["*"]``
# exposes every tool the server registers.
COPILOT_ENTRY_EXTRA: Mapping[str, Any] = {"type": "stdio", "tools": ["*"]}
DROID_ENTRY_EXTRA: Mapping[str, Any] = {"type": "stdio"}


def command_and_args(dev_path: str | None) -> tuple[str, list[str]]:
    if dev_path is not None:
        abs_path = str(Path(dev_path).expanduser().resolve())
        return "uv", ["run", "--directory", abs_path, "python", "-m", "rlaif", "serve"]
    return "rlaif", ["serve"]


def stdio_entry(command: str, args: list[str], extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The ``mcpServers.rlaif`` entry: ``command`` + ``args`` plus per-client
    ``extra`` keys. ``type`` leads when present, since it names the transport
    and reads first in every client's docs; the remaining extras trail."""
    extra_fields: dict[str, Any] = copy.deepcopy(dict(extra)) if extra else {}
    entry: dict[str, Any] = {}
    if "type" in extra_fields:
        entry["type"] = extra_fields.pop("type")
    entry["command"] = command
    entry["args"] = list(args)
    entry.update(extra_fields)
    return entry


def json_mcp_servers_builder(command: str, args: list[str]) -> str:
    return json.dumps(
        {"mcpServers": {"rlaif": stdio_entry(command, args)}},
        indent=2,
    )


def copilot_builder(command: str, args: list[str]) -> str:
    return json.dumps(
        {"mcpServers": {"rlaif": stdio_entry(command, args, COPILOT_ENTRY_EXTRA)}},
        indent=2,
    )


def droid_builder(command: str, args: list[str]) -> str:
    return json.dumps(
        {"mcpServers": {"rlaif": stdio_entry(command, args, DROID_ENTRY_EXTRA)}},
        indent=2,
    )


def codex_builder(command: str, args: list[str]) -> str:
    # Also serves grok: Grok Build's ``~/.grok/config.toml`` uses the same
    # ``[mcp_servers.<name>]`` table with ``command`` + ``args``.
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

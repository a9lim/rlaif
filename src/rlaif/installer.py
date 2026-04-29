"""Auto-install / uninstall rlaif into MCP client config files (phase 1).

Supported (auto-install + auto-uninstall):
  claude-desktop, claude-code, cursor, windsurf, antigravity

All five use the standard ``mcpServers`` JSON schema with a dedicated
config file, making safe round-trip merge straightforward (stdlib ``json``
handles them; nothing here touches comments or non-JSON formats).

Unsupported (manual paste only):
  codex (TOML), hermes (YAML), opencode (JSONC), vscode (JSONC),
  zed (JSONC inside a multi-purpose settings file).

These either need round-trip-aware parsers we don't depend on, or share a
file with unrelated user state (zed). For these, ``install`` and
``uninstall`` exit nonzero with a hint pointing at ``rlaif snippet`` /
manual edit. See README and CLAUDE.md for the full rationale.

Conflict policy:
  If ``rlaif`` is already registered with a *different* command/args than
  we'd write, refuse with exit code 1 unless ``--force`` is passed.
  An identical existing entry is treated as a noop (idempotent).

Backup policy:
  Single ``<file>.rlaif.bak`` overwritten on each mutating run. We do not
  keep timestamped history — re-running install/uninstall a third time
  loses the original. If you need history, use git or copy the .bak
  before re-running.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rlaif.snippet import command_and_args

SUPPORTED: tuple[str, ...] = (
    "claude-desktop",
    "claude-code",
    "cursor",
    "windsurf",
    "antigravity",
)


def _claude_desktop_path() -> Path:
    home = Path.home()
    # Widening to ``str`` defeats pyright's host-specific narrowing of
    # ``sys.platform`` so the non-host branches are not flagged unreachable.
    plat: str = sys.platform
    if plat == "darwin":
        return (
            home
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if plat == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


_PATHS: dict[str, Callable[[], Path]] = {
    "claude-desktop": _claude_desktop_path,
    "claude-code": lambda: Path.home() / ".claude.json",
    "cursor": lambda: Path.home() / ".cursor" / "mcp.json",
    "windsurf": lambda: Path.home() / ".codeium" / "windsurf" / "mcp_config.json",
    "antigravity": lambda: Path.home() / ".gemini" / "antigravity" / "mcp_config.json",
}


class InstallError(Exception):
    """Raised on user-facing install/uninstall failures."""


def _read_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise InstallError(f"could not read {path}: {e}") from e
    if not text.strip():
        return {}
    try:
        loaded: Any = json.loads(text)
    except json.JSONDecodeError as e:
        raise InstallError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(loaded, dict):
        raise InstallError(f"{path} top-level is not a JSON object")
    return loaded  # pyright: ignore[reportUnknownVariableType]


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".rlaif.bak")


def _backup(path: Path) -> Path | None:
    """Single backup, overwritten on each mutating run. Returns the backup
    path if one was made (file existed), else ``None``."""
    if not path.exists():
        return None
    bak = _backup_path(path)
    shutil.copy2(path, bak)
    return bak


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    if path.exists():
        existing_mode = path.stat().st_mode & 0o777
        os.chmod(tmp, existing_mode)
    else:
        os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _serialize(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


def _desired_entry(dev_path: str | None) -> dict[str, Any]:
    command, args = command_and_args(dev_path)
    return {"command": command, "args": args}


def _entries_equal(a: Any, b: Any) -> bool:
    """Strict equality on the {command, args} shape we manage. Extra keys
    on the user's side count as a conflict — we don't silently strip them."""
    return a == b


def _print_unsupported_install(client: str) -> None:
    print(
        f"auto-install not supported for {client!r}.\n"
        f"the config format requires a round-trip-aware parser we don't "
        f"depend on, or shares a file with unrelated user state.\n"
        f"run: rlaif snippet {client}\n"
        f"and paste the output into the documented config file.",
        file=sys.stderr,
    )


def _print_unsupported_uninstall(client: str) -> None:
    print(
        f"auto-uninstall not supported for {client!r}.\n"
        f"open the documented config file and remove the `rlaif` entry "
        f"manually. see `rlaif snippet {client}` for the location.",
        file=sys.stderr,
    )


def install(
    client: str,
    *,
    dev_path: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    if client not in SUPPORTED:
        _print_unsupported_install(client)
        return 2

    path = _PATHS[client]()
    try:
        existing = _read_existing(path)
    except InstallError as e:
        print(str(e), file=sys.stderr)
        return 1

    servers_any: Any = existing.get("mcpServers")
    if servers_any is None:
        servers: dict[str, Any] = {}
        existing["mcpServers"] = servers
    elif isinstance(servers_any, dict):
        servers = servers_any  # pyright: ignore[reportUnknownVariableType]
    else:
        print(
            f"{path} has a non-object `mcpServers` field — refusing to touch it.",
            file=sys.stderr,
        )
        return 1

    desired = _desired_entry(dev_path)
    current = servers.get("rlaif")

    if current is not None and not _entries_equal(current, desired):
        if not force:
            print(
                f"{path} already has a different `rlaif` entry. "
                f"re-run with --force to overwrite, or remove it manually.",
                file=sys.stderr,
            )
            return 1

    if _entries_equal(current, desired):
        print(f"# rlaif already installed in {path} (no change)")
        return 0

    servers["rlaif"] = desired
    new_text = _serialize(existing)

    if dry_run:
        print(f"# would write to {path}:")
        print(new_text, end="")
        return 0

    bak = _backup(path)
    _atomic_write(path, new_text)
    print(f"# installed rlaif into {path}")
    if bak is not None:
        print(f"# backup at {bak}")
    return 0


def uninstall(client: str, *, dry_run: bool = False) -> int:
    if client not in SUPPORTED:
        _print_unsupported_uninstall(client)
        return 2

    path = _PATHS[client]()
    if not path.exists():
        print(f"# {path} does not exist; nothing to remove.")
        return 0

    try:
        existing = _read_existing(path)
    except InstallError as e:
        print(str(e), file=sys.stderr)
        return 1

    servers_any: Any = existing.get("mcpServers")
    if not isinstance(servers_any, dict):
        print(f"# rlaif is not installed in {path}; nothing to remove.")
        return 0
    servers: dict[str, Any] = servers_any  # pyright: ignore[reportUnknownVariableType]

    if "rlaif" not in servers:
        print(f"# rlaif is not installed in {path}; nothing to remove.")
        return 0

    del servers["rlaif"]
    new_text = _serialize(existing)

    if dry_run:
        print(f"# would write to {path}:")
        print(new_text, end="")
        return 0

    bak = _backup(path)
    _atomic_write(path, new_text)
    print(f"# removed rlaif from {path}")
    if bak is not None:
        print(f"# backup at {bak}")
    return 0

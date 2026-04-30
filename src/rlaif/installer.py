"""Auto-install / uninstall rlaif into MCP client config files.

Supported (auto-install + auto-uninstall):
  claude-desktop, claude-code, cursor, windsurf, antigravity (JSON),
  opencode (JSON, opencode-specific schema), codex (TOML), hermes (YAML).

Each format gets a ``FormatAdapter`` that owns round-trip semantics —
comments, key order, quoting, surrounding user state — so installing onto
a populated config is non-destructive:

* stdlib ``json`` for the JSON clients (no comments to preserve).
* ``tomlkit`` for codex (preserves comments + table order + quoting).
* ``ruamel.yaml`` for hermes (preserves comments + key order + quoting).

Unsupported (manual paste only):
  vscode (JSONC), zed (JSONC inside multi-purpose settings).

Python's JSONC story has no mature comment-preserving JSONC writer. zed
additionally shares its ``settings.json`` with arbitrary editor state
(themes, keybindings, language servers), so the blast radius is
unacceptable even with a parser. For these clients ``install`` and
``uninstall`` exit nonzero with a hint pointing at ``rlaif snippet``.

Opencode supports both ``opencode.json`` (plain JSON) and
``opencode.jsonc`` (with comments). We auto-install only against the
``.json`` variant — if the config file we touch contains comments, we
detect the JSON parse failure and redirect the operator to ``snippet``.

Conflict policy:
  If ``rlaif`` is already registered with a *different* entry than we'd
  write, refuse with exit code 1 unless ``--force`` is passed. An identical
  existing entry is treated as a noop (idempotent).

Backup policy:
  Single ``<file>.rlaif.bak`` overwritten on each mutating run. We do not
  keep timestamped history — re-running install/uninstall a third time
  loses the original. If you need history, use git or copy the .bak
  before re-running.
"""

# tomlkit and ruamel.yaml are partially typed — their public surface
# returns Any-ish dict/list-like objects. Suppressing the unknown-type
# noise here lets the rest of the file stay strict-typed without
# fighting the libraries' own type stubs at every call site.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import io
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Literal

import tomlkit
import tomlkit.exceptions
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from rlaif.snippet import command_and_args


def claude_desktop_path() -> Path:
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


class InstallError(Exception):
    """Raised on user-facing install/uninstall failures."""


# ---------------------------------------------------------------------------
# format adapters — each owns parse/serialize and rlaif-entry manipulation
# for one config format. Tests covering round-trip live in test_installer.py.
# ---------------------------------------------------------------------------


class FormatAdapter:
    """Abstract base. Concrete adapters preserve user comments and key
    order across a noop install — that property is what makes auto-mutation
    of TOML/YAML configs safe."""

    name: str = ""

    def parse(self, text: str, path: Path) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def empty(self) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def serialize(self, doc: Any) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def get_rlaif(self, doc: Any) -> Any | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def set_rlaif(self, doc: Any, command: str, args: list[str]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def remove_rlaif(self, doc: Any) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def matches_desired(
        self, current: Any, command: str, args: list[str]
    ) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError


class JsonAdapter(FormatAdapter):
    """JSON via stdlib. No comments to preserve.

    Schema: top-level ``mcpServers`` object with a ``rlaif`` key whose
    value is exactly ``{"command": ..., "args": [...]}``.
    """

    name = "JSON"

    def parse(self, text: str, path: Path) -> dict[str, Any]:
        if not text.strip():
            return {}
        try:
            loaded: Any = json.loads(text)
        except json.JSONDecodeError as e:
            raise InstallError(f"{path} is not valid JSON: {e}") from e
        if not isinstance(loaded, dict):
            raise InstallError(f"{path} top-level is not a JSON object")
        return loaded  # pyright: ignore[reportUnknownVariableType]

    def empty(self) -> dict[str, Any]:
        return {}

    def serialize(self, doc: Any) -> str:
        return json.dumps(doc, indent=2) + "\n"

    def _servers(
        self, doc: dict[str, Any], *, create: bool
    ) -> dict[str, Any] | None:
        servers_any: Any = doc.get("mcpServers")
        if servers_any is None:
            if not create:
                return None
            new: dict[str, Any] = {}
            doc["mcpServers"] = new
            return new
        if not isinstance(servers_any, dict):
            raise InstallError(
                "non-object `mcpServers` field — refusing to touch it."
            )
        return servers_any  # pyright: ignore[reportUnknownVariableType]

    def get_rlaif(self, doc: Any) -> Any | None:
        servers = self._servers(doc, create=False)
        if servers is None:
            return None
        return servers.get("rlaif")

    def set_rlaif(self, doc: Any, command: str, args: list[str]) -> None:
        servers = self._servers(doc, create=True)
        assert servers is not None
        servers["rlaif"] = {"command": command, "args": list(args)}

    def remove_rlaif(self, doc: Any) -> bool:
        servers = self._servers(doc, create=False)
        if servers is None or "rlaif" not in servers:
            return False
        del servers["rlaif"]
        return True

    def matches_desired(self, current: Any, command: str, args: list[str]) -> bool:
        # Strict equality — extra keys on the user's side count as a conflict
        # so we don't silently strip them on overwrite.
        return current == {"command": command, "args": list(args)}


class OpencodeJsonAdapter(FormatAdapter):
    """opencode-specific JSON schema (plain JSON only — JSONC redirects to snippet).

    Schema:
        {
          "$schema": "https://opencode.ai/config.json",
          "mcp": {
            "rlaif": {
              "type": "local",
              "command": [<command>, *<args>],   # array, not separate command+args
              "enabled": true
            }
          }
        }

    The opencode docs accept both ``opencode.json`` (plain JSON) and
    ``opencode.jsonc`` (with comments). We only auto-write the former;
    if the operator is on JSONC, the JSON parser will error and the
    install path redirects to ``rlaif snippet opencode``.

    We preserve any non-rlaif content (other ``mcp.*`` entries, top-level
    keys like ``model``, ``$schema``, etc.) untouched. The ``$schema`` key
    is added on first install only when the file was empty/new — we don't
    want to inject it into an existing user file that chose to omit it.
    """

    name = "JSON"

    SCHEMA_URL = "https://opencode.ai/config.json"

    def parse(self, text: str, path: Path) -> dict[str, Any]:
        if not text.strip():
            return {}
        try:
            loaded: Any = json.loads(text)
        except json.JSONDecodeError as e:
            raise InstallError(
                f"{path} is not valid JSON: {e}. opencode supports both "
                f"opencode.json (plain JSON) and opencode.jsonc (with "
                f"comments); auto-install only handles plain JSON. "
                f"Run `rlaif snippet opencode` and paste the result manually."
            ) from e
        if not isinstance(loaded, dict):
            raise InstallError(f"{path} top-level is not a JSON object")
        return loaded  # pyright: ignore[reportUnknownVariableType]

    def empty(self) -> dict[str, Any]:
        return {"$schema": self.SCHEMA_URL, "mcp": {}}

    def serialize(self, doc: Any) -> str:
        return json.dumps(doc, indent=2) + "\n"

    def _mcp(
        self, doc: dict[str, Any], *, create: bool
    ) -> dict[str, Any] | None:
        mcp_any: Any = doc.get("mcp")
        if mcp_any is None:
            if not create:
                return None
            new: dict[str, Any] = {}
            doc["mcp"] = new
            return new
        if not isinstance(mcp_any, dict):
            raise InstallError(
                "non-object `mcp` field — refusing to touch it."
            )
        return mcp_any  # pyright: ignore[reportUnknownVariableType]

    def get_rlaif(self, doc: Any) -> Any | None:
        mcp = self._mcp(doc, create=False)
        if mcp is None:
            return None
        return mcp.get("rlaif")

    def set_rlaif(self, doc: Any, command: str, args: list[str]) -> None:
        mcp = self._mcp(doc, create=True)
        assert mcp is not None
        mcp["rlaif"] = {
            "type": "local",
            "command": [command, *args],
            "enabled": True,
        }

    def remove_rlaif(self, doc: Any) -> bool:
        mcp = self._mcp(doc, create=False)
        if mcp is None or "rlaif" not in mcp:
            return False
        del mcp["rlaif"]
        return True

    def matches_desired(self, current: Any, command: str, args: list[str]) -> bool:
        if not isinstance(current, dict):
            return False
        if set(current.keys()) != {"type", "command", "enabled"}:
            return False
        if current.get("type") != "local":
            return False
        if current.get("enabled") is not True:
            return False
        cur_cmd: Any = current.get("command")
        try:
            return list(cur_cmd) == [command, *args]
        except TypeError:
            return False


class TomlAdapter(FormatAdapter):
    """TOML via tomlkit (round-trip; preserves comments, key order, quoting).

    Schema (codex): ``[mcp_servers.rlaif]`` table with ``command`` + ``args``.
    tomlkit auto-promotes nested tables to dotted-key headers, so a fresh
    install produces the same shape as ``rlaif snippet codex``.
    """

    name = "TOML"

    def parse(self, text: str, path: Path) -> Any:
        try:
            return tomlkit.parse(text)
        except tomlkit.exceptions.TOMLKitError as e:
            raise InstallError(f"{path} is not valid TOML: {e}") from e

    def empty(self) -> Any:
        return tomlkit.document()

    def serialize(self, doc: Any) -> str:
        out: str = tomlkit.dumps(doc)
        if not out.endswith("\n"):
            out += "\n"
        return out

    def _servers(self, doc: Any, *, create: bool) -> Any | None:
        servers: Any = doc.get("mcp_servers")
        if servers is None:
            if not create:
                return None
            servers = tomlkit.table()
            doc["mcp_servers"] = servers
            return servers
        if not isinstance(servers, dict):
            raise InstallError(
                "non-table `mcp_servers` field — refusing to touch it."
            )
        return servers

    def get_rlaif(self, doc: Any) -> Any | None:
        servers = self._servers(doc, create=False)
        if servers is None:
            return None
        rlaif: Any = servers.get("rlaif")
        return rlaif

    def set_rlaif(self, doc: Any, command: str, args: list[str]) -> None:
        servers = self._servers(doc, create=True)
        assert servers is not None
        rlaif: Any = tomlkit.table()
        rlaif["command"] = command
        rlaif["args"] = list(args)
        servers["rlaif"] = rlaif

    def remove_rlaif(self, doc: Any) -> bool:
        servers = self._servers(doc, create=False)
        if servers is None or "rlaif" not in servers:
            return False
        del servers["rlaif"]
        return True

    def matches_desired(self, current: Any, command: str, args: list[str]) -> bool:
        if not isinstance(current, dict):
            return False
        if set(current.keys()) != {"command", "args"}:
            return False
        if current.get("command") != command:
            return False
        cur_args: Any = current.get("args")
        try:
            return list(cur_args) == list(args)
        except TypeError:
            return False


# Hermes scopes each MCP server to a tool subset. The snippet for hermes
# in snippet.py and the install entry must agree on this shape — both are
# the contract the user sees.
_HERMES_TOOLS_INCLUDE: list[str] = ["rlaif_info", "rlaif_log", "rlaif"]


class YamlAdapter(FormatAdapter):
    """YAML via ruamel.yaml (round-trip; preserves comments, key order).

    Schema (hermes): ``mcp_servers.rlaif`` map with ``command``, ``args``,
    and a ``tools`` block scoping rlaif to its three tools (no prompts,
    no resources).
    """

    name = "YAML"

    def __init__(self) -> None:
        self._yaml: YAML = YAML()
        self._yaml.preserve_quotes = True
        self._yaml.indent(mapping=2, sequence=4, offset=2)

    def parse(self, text: str, path: Path) -> Any:
        try:
            loaded: Any = self._yaml.load(text)
        except Exception as e:
            raise InstallError(f"{path} is not valid YAML: {e}") from e
        if loaded is None:
            return CommentedMap()
        if not isinstance(loaded, dict):
            raise InstallError(f"{path} top-level is not a YAML mapping")
        return loaded

    def empty(self) -> Any:
        return CommentedMap()

    def serialize(self, doc: Any) -> str:
        buf = io.StringIO()
        self._yaml.dump(doc, buf)
        return buf.getvalue()

    def _servers(self, doc: Any, *, create: bool) -> Any | None:
        servers: Any = doc.get("mcp_servers")
        if servers is None:
            if not create:
                return None
            servers = CommentedMap()
            doc["mcp_servers"] = servers
            return servers
        if not isinstance(servers, dict):
            raise InstallError(
                "non-mapping `mcp_servers` field — refusing to touch it."
            )
        return servers

    def get_rlaif(self, doc: Any) -> Any | None:
        servers = self._servers(doc, create=False)
        if servers is None:
            return None
        rlaif: Any = servers.get("rlaif")
        return rlaif

    def set_rlaif(self, doc: Any, command: str, args: list[str]) -> None:
        servers = self._servers(doc, create=True)
        assert servers is not None
        entry: Any = CommentedMap()
        entry["command"] = command
        entry["args"] = list(args)
        tools: Any = CommentedMap()
        tools["include"] = list(_HERMES_TOOLS_INCLUDE)
        tools["prompts"] = False
        tools["resources"] = False
        entry["tools"] = tools
        servers["rlaif"] = entry

    def remove_rlaif(self, doc: Any) -> bool:
        servers = self._servers(doc, create=False)
        if servers is None or "rlaif" not in servers:
            return False
        del servers["rlaif"]
        return True

    def matches_desired(self, current: Any, command: str, args: list[str]) -> bool:
        if not isinstance(current, dict):
            return False
        if set(current.keys()) != {"command", "args", "tools"}:
            return False
        if current.get("command") != command:
            return False
        cur_args: Any = current.get("args")
        try:
            if list(cur_args) != list(args):
                return False
        except TypeError:
            return False
        tools: Any = current.get("tools")
        if not isinstance(tools, dict):
            return False
        if set(tools.keys()) != {"include", "prompts", "resources"}:
            return False
        cur_include: Any = tools.get("include")
        try:
            if list(cur_include) != list(_HERMES_TOOLS_INCLUDE):
                return False
        except TypeError:
            return False
        if tools.get("prompts") is not False:
            return False
        if tools.get("resources") is not False:
            return False
        return True


# ---------------------------------------------------------------------------
# common file ops (format-agnostic)
# ---------------------------------------------------------------------------


def _read_existing(path: Path, adapter: FormatAdapter) -> Any:
    if not path.exists():
        return adapter.empty()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise InstallError(f"could not read {path}: {e}") from e
    if not text.strip():
        return adapter.empty()
    return adapter.parse(text, path)


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


def _print_unsupported(
    client: str, *, action: Literal["install", "uninstall"]
) -> None:
    if action == "install":
        print(
            f"auto-install not supported for {client!r}.\n"
            f"the config format requires a round-trip-aware parser we don't "
            f"depend on, or shares a file with unrelated user state.\n"
            f"run: rlaif snippet {client}\n"
            f"and paste the output into the documented config file.",
            file=sys.stderr,
        )
        return
    print(
        f"auto-uninstall not supported for {client!r}.\n"
        f"open the documented config file and remove the `rlaif` entry "
        f"manually. see `rlaif snippet {client}` for the location.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------


def install(
    client: str,
    *,
    dev_path: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    # Deferred import: rlaif._clients imports the adapter classes defined
    # above, so the dependency runs leaf -> registry, not the reverse.
    from rlaif._clients import CLIENTS_REGISTRY

    record = CLIENTS_REGISTRY.get(client)
    if record is None or not record.auto_installable:
        _print_unsupported(client, action="install")
        return 2
    assert record.path_fn is not None and record.format_adapter is not None
    path = record.path_fn()
    adapter = record.format_adapter

    try:
        doc = _read_existing(path, adapter)
    except InstallError as e:
        print(str(e), file=sys.stderr)
        return 1

    command, args = command_and_args(dev_path)

    try:
        current = adapter.get_rlaif(doc)
    except InstallError as e:
        print(f"{path}: {e}", file=sys.stderr)
        return 1

    if current is not None and adapter.matches_desired(current, command, args):
        print(f"# rlaif already installed in {path} (no change)")
        return 0

    if current is not None and not force:
        print(
            f"{path} already has a different `rlaif` entry. "
            f"re-run with --force to overwrite, or remove it manually.",
            file=sys.stderr,
        )
        return 1

    try:
        adapter.set_rlaif(doc, command, args)
    except InstallError as e:
        print(f"{path}: {e}", file=sys.stderr)
        return 1
    new_text = adapter.serialize(doc)

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
    from rlaif._clients import CLIENTS_REGISTRY

    record = CLIENTS_REGISTRY.get(client)
    if record is None or not record.auto_installable:
        _print_unsupported(client, action="uninstall")
        return 2
    assert record.path_fn is not None and record.format_adapter is not None
    path = record.path_fn()
    adapter = record.format_adapter

    if not path.exists():
        print(f"# {path} does not exist; nothing to remove.")
        return 0

    try:
        doc = _read_existing(path, adapter)
    except InstallError as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        removed = adapter.remove_rlaif(doc)
    except InstallError as e:
        print(f"{path}: {e}", file=sys.stderr)
        return 1
    if not removed:
        print(f"# rlaif is not installed in {path}; nothing to remove.")
        return 0

    new_text = adapter.serialize(doc)

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

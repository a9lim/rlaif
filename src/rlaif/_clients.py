"""Single source of truth for per-MCP-client metadata.

Lives here (not in snippet.py or installer.py) so neither of those modules
has to know about the other's data; they each look the client up by name
and consume only the fields they need. Keeping ``tomlkit`` / ``ruamel.yaml``
imports confined to ``installer.py`` rules out putting the registry there;
having ``snippet.py`` own it would force snippet to import installer at
module load just to populate ``format_adapter``. So a separate leaf module.

Adding a client = one entry here, plus (if auto-installable) the matching
adapter/path-fn in ``installer.py`` and the builder in ``snippet.py``. The
registry's iteration order is the order operators see in ``rlaif init`` and
``rlaif snippet --help``, so it doubles as the canonical client list. A path
that honors an env override (``COPILOT_HOME``, ``XDG_CONFIG_HOME``,
``APPDATA``) is a named function in ``installer.py``, not a lambda, so tests
can monkeypatch the environment.

Imports flow strictly leaf-ward: this module imports from ``installer`` and
``snippet`` at top-level. ``installer`` and ``snippet`` defer their imports
of ``CLIENTS_REGISTRY`` to function-call time to keep the dependency
acyclic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rlaif.installer import (
    FormatAdapter,
    JsonAdapter,
    OpencodeJsonAdapter,
    TomlAdapter,
    YamlAdapter,
    claude_desktop_path,
    copilot_path,
    devin_path,
)
from rlaif.snippet import (
    COPILOT_ENTRY_EXTRA,
    DROID_ENTRY_EXTRA,
    Builder,
    codex_builder,
    copilot_builder,
    droid_builder,
    hermes_builder,
    json_mcp_servers_builder,
    opencode_builder,
    vscode_builder,
    zed_builder,
)


@dataclass(frozen=True)
class ClientRecord:
    """Everything we know about one MCP client.

    ``path_fn`` and ``format_adapter`` are populated together (both set =
    auto-installable) or both ``None`` (snippet-only). vscode and zed stay
    snippet-only because their config formats (JSONC, multi-purpose
    settings.json) aren't safely round-trippable; vscode's user-level config
    is reachable through copilot, which is auto-installable.
    """

    name: str
    location_hint: str
    snippet_builder: Builder
    path_fn: Callable[[], Path] | None
    format_adapter: FormatAdapter | None

    @property
    def auto_installable(self) -> bool:
        return self.path_fn is not None and self.format_adapter is not None


# Singletons — adapters are stateless past construction, so one instance
# per format is enough. Kept module-private; consumers reach them via the
# registry. copilot and droid get their own JSON instance because their
# entries carry extra keys that the conflict check must also expect.
_JSON = JsonAdapter()
_JSON_COPILOT = JsonAdapter(COPILOT_ENTRY_EXTRA)
_JSON_DROID = JsonAdapter(DROID_ENTRY_EXTRA)
_OPENCODE_JSON = OpencodeJsonAdapter()
_TOML = TomlAdapter()
_YAML = YamlAdapter()


def _records() -> tuple[ClientRecord, ...]:
    return (
        ClientRecord(
            name="claude-desktop",
            location_hint="~/Library/Application Support/Claude/claude_desktop_config.json",
            snippet_builder=json_mcp_servers_builder,
            path_fn=claude_desktop_path,
            format_adapter=_JSON,
        ),
        ClientRecord(
            name="claude-code",
            location_hint="~/.claude.json (user) or .claude.json (project)",
            snippet_builder=json_mcp_servers_builder,
            path_fn=lambda: Path.home() / ".claude.json",
            format_adapter=_JSON,
        ),
        ClientRecord(
            name="codex",
            location_hint="~/.codex/config.toml",
            snippet_builder=codex_builder,
            path_fn=lambda: Path.home() / ".codex" / "config.toml",
            format_adapter=_TOML,
        ),
        ClientRecord(
            name="copilot",
            location_hint="~/.copilot/mcp-config.json (or $COPILOT_HOME/mcp-config.json; Copilot CLI, also read by VS Code)",
            snippet_builder=copilot_builder,
            path_fn=copilot_path,
            format_adapter=_JSON_COPILOT,
        ),
        ClientRecord(
            name="grok",
            location_hint="~/.grok/config.toml",
            snippet_builder=codex_builder,
            path_fn=lambda: Path.home() / ".grok" / "config.toml",
            format_adapter=_TOML,
        ),
        ClientRecord(
            name="antigravity",
            location_hint="~/.gemini/config/mcp_config.json (shared by the Antigravity IDE and the `agy` CLI)",
            snippet_builder=json_mcp_servers_builder,
            path_fn=lambda: Path.home() / ".gemini" / "config" / "mcp_config.json",
            format_adapter=_JSON,
        ),
        ClientRecord(
            name="cursor",
            location_hint="~/.cursor/mcp.json (user) or .cursor/mcp.json (project)",
            snippet_builder=json_mcp_servers_builder,
            path_fn=lambda: Path.home() / ".cursor" / "mcp.json",
            format_adapter=_JSON,
        ),
        ClientRecord(
            name="devin",
            location_hint=r"~/.config/devin/mcp_config.json (or $XDG_CONFIG_HOME/devin/; %APPDATA%\devin\ on Windows)",
            snippet_builder=json_mcp_servers_builder,
            path_fn=devin_path,
            format_adapter=_JSON,
        ),
        ClientRecord(
            name="kiro",
            location_hint="~/.kiro/settings/mcp.json (shared by the Kiro IDE and kiro-cli)",
            snippet_builder=json_mcp_servers_builder,
            path_fn=lambda: Path.home() / ".kiro" / "settings" / "mcp.json",
            format_adapter=_JSON,
        ),
        ClientRecord(
            name="junie",
            location_hint="~/.junie/mcp/mcp.json (shared by the JetBrains IDE plugin and the CLI)",
            snippet_builder=json_mcp_servers_builder,
            path_fn=lambda: Path.home() / ".junie" / "mcp" / "mcp.json",
            format_adapter=_JSON,
        ),
        ClientRecord(
            name="droid",
            location_hint="~/.factory/mcp.json",
            snippet_builder=droid_builder,
            path_fn=lambda: Path.home() / ".factory" / "mcp.json",
            format_adapter=_JSON_DROID,
        ),
        ClientRecord(
            name="opencode",
            location_hint="opencode.json (project) or ~/.config/opencode/opencode.json (global)",
            snippet_builder=opencode_builder,
            path_fn=lambda: Path.home() / ".config" / "opencode" / "opencode.json",
            format_adapter=_OPENCODE_JSON,
        ),
        ClientRecord(
            name="hermes",
            location_hint="~/.hermes/config.yaml",
            snippet_builder=hermes_builder,
            path_fn=lambda: Path.home() / ".hermes" / "config.yaml",
            format_adapter=_YAML,
        ),
        ClientRecord(
            name="lmstudio",
            location_hint="~/.lmstudio/mcp.json",
            snippet_builder=json_mcp_servers_builder,
            path_fn=lambda: Path.home() / ".lmstudio" / "mcp.json",
            format_adapter=_JSON,
        ),
        ClientRecord(
            name="vscode",
            location_hint=".vscode/mcp.json (workspace); for the user-level config run `rlaif install copilot` (VS Code prefers ~/.copilot/mcp-config.json)",
            snippet_builder=vscode_builder,
            path_fn=None,
            format_adapter=None,
        ),
        ClientRecord(
            name="zed",
            location_hint="~/.config/zed/settings.json (merge fragment into top level)",
            snippet_builder=zed_builder,
            path_fn=None,
            format_adapter=None,
        ),
    )


CLIENTS_REGISTRY: dict[str, ClientRecord] = {r.name: r for r in _records()}

# Backwards-friendly views computed once. Order mirrors CLIENTS_REGISTRY's
# insertion order; that is the order operators see in `rlaif init`.
CLIENTS: tuple[str, ...] = tuple(CLIENTS_REGISTRY)
INSTALL_SUPPORTED: tuple[str, ...] = tuple(name for name, rec in CLIENTS_REGISTRY.items() if rec.auto_installable)

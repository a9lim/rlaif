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
``rlaif snippet --help``, so it doubles as the canonical client list.

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
)
from rlaif.snippet import (
    Builder,
    codex_builder,
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
    settings.json) aren't safely round-trippable.
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
# registry.
_JSON = JsonAdapter()
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
            name="hermes",
            location_hint="~/.hermes/config.yaml",
            snippet_builder=hermes_builder,
            path_fn=lambda: Path.home() / ".hermes" / "config.yaml",
            format_adapter=_YAML,
        ),
        ClientRecord(
            name="antigravity",
            location_hint="~/.gemini/antigravity/mcp_config.json",
            snippet_builder=json_mcp_servers_builder,
            path_fn=lambda: Path.home() / ".gemini" / "antigravity" / "mcp_config.json",
            format_adapter=_JSON,
        ),
        ClientRecord(
            name="opencode",
            location_hint="opencode.json (project) or ~/.config/opencode/opencode.json (global)",
            snippet_builder=opencode_builder,
            path_fn=lambda: Path.home() / ".config" / "opencode" / "opencode.json",
            format_adapter=_OPENCODE_JSON,
        ),
        ClientRecord(
            name="cursor",
            location_hint="~/.cursor/mcp.json (user) or .cursor/mcp.json (project)",
            snippet_builder=json_mcp_servers_builder,
            path_fn=lambda: Path.home() / ".cursor" / "mcp.json",
            format_adapter=_JSON,
        ),
        ClientRecord(
            name="windsurf",
            location_hint="~/.codeium/windsurf/mcp_config.json",
            snippet_builder=json_mcp_servers_builder,
            path_fn=lambda: Path.home() / ".codeium" / "windsurf" / "mcp_config.json",
            format_adapter=_JSON,
        ),
        ClientRecord(
            name="vscode",
            location_hint=".vscode/mcp.json (workspace) or user mcp.json",
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
INSTALL_SUPPORTED: tuple[str, ...] = tuple(
    name for name, rec in CLIENTS_REGISTRY.items() if rec.auto_installable
)

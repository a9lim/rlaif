"""Tests for `rlaif install` / `rlaif uninstall` (14 supported clients).

Ten ``mcpServers`` JSON clients (claude-desktop, claude-code, cursor,
antigravity, devin, kiro, junie, lmstudio, plus copilot and droid whose
entries carry extra keys), opencode (JSON, own schema), codex and grok
(TOML, via tomlkit), and hermes (YAML, via ruamel.yaml). All tests redirect
``Path.home()`` (and on macOS the claude-desktop sub-path) into a tmp dir
and clear the path-override env vars so they never touch real user state.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import tomlkit
from ruamel.yaml import YAML

from rlaif._clients import CLIENTS, CLIENTS_REGISTRY
from rlaif._clients import INSTALL_SUPPORTED as SUPPORTED
from rlaif.cli import main
from rlaif.installer import copilot_path, devin_path, install


# Tests pre-refactor used a ``_PATHS[client]()`` dict keyed on client name.
# Post-refactor the path resolvers live on each ``ClientRecord``; this
# tiny shim preserves the call-site shape without churn across the file.
class _PathLookup:
    def __getitem__(self, client: str) -> Callable[[], Path]:
        record = CLIENTS_REGISTRY[client]
        assert record.path_fn is not None, f"{client} is snippet-only"
        return record.path_fn


_PATHS = _PathLookup()

# Clients that share the top-level ``mcpServers`` JSON schema.
JSON_CLIENTS = (
    "claude-desktop",
    "claude-code",
    "cursor",
    "antigravity",
    "devin",
    "kiro",
    "junie",
    "lmstudio",
    "copilot",
    "droid",
)
# Entry keys beyond ``command``/``args`` that a client requires; both the
# written entry and the conflict check must carry them.
EXTRA_ENTRY_KEYS: dict[str, dict[str, Any]] = {
    "copilot": {"type": "stdio", "tools": ["*"]},
    "droid": {"type": "stdio"},
}
# Where each new or moved client's config lands under a fake home (POSIX
# layout; the env-override cases are tested separately).
HOME_RELATIVE_PATHS: dict[str, tuple[str, ...]] = {
    "antigravity": (".gemini", "config", "mcp_config.json"),
    "devin": (".config", "devin", "mcp_config.json"),
    "grok": (".grok", "config.toml"),
    "copilot": (".copilot", "mcp-config.json"),
    "kiro": (".kiro", "settings", "mcp.json"),
    "junie": (".junie", "mcp", "mcp.json"),
    "droid": (".factory", "mcp.json"),
    "lmstudio": (".lmstudio", "mcp.json"),
}
# opencode uses JSON but a different top-level schema (`mcp.<name>` with
# `command` as an array). Tests that build a rlaif entry assertion need to
# branch on whether the client is opencode.
TRULY_UNSUPPORTED = ("vscode", "zed")


# Helpers return Any because tomlkit / ruamel surface dict-and-list-like
# wrappers that don't satisfy strict invariant container protocols. These
# are tests, not load-bearing — Any keeps the assertion sites readable.
def _load_toml(path: Path) -> Any:
    return tomlkit.parse(path.read_text())


def _load_yaml(path: Path) -> Any:
    y = YAML()
    return y.load(io.StringIO(path.read_text()))


def _read_rlaif_entry(client: str, path: Path) -> Any:
    """Format-aware lookup of the rlaif entry for assertion purposes."""
    if client in {"codex", "grok"}:
        return _load_toml(path)["mcp_servers"]["rlaif"]
    if client == "hermes":
        return _load_yaml(path)["mcp_servers"]["rlaif"]
    if client == "opencode":
        return json.loads(path.read_text())["mcp"]["rlaif"]
    return json.loads(path.read_text())["mcpServers"]["rlaif"]


def _entry_command_args(client: str, entry: Any) -> tuple[str, list[str]]:
    """Pull (command, args) out of a client's entry shape, so tests can
    assert the same logical thing across schemas (opencode merges them
    into a single ``command`` array; the others keep them split)."""
    if client == "opencode":
        cmd_arr = list(entry["command"])
        return cmd_arr[0], cmd_arr[1:]
    return entry["command"], list(entry["args"])


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # claude-desktop and devin read $APPDATA on Windows, devin honors
    # $XDG_CONFIG_HOME elsewhere, and copilot honors $COPILOT_HOME. Clear them
    # so a developer's real environment can't redirect the tests.
    for var in ("APPDATA", "XDG_CONFIG_HOME", "COPILOT_HOME"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# install — supported clients
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client", list(SUPPORTED))
def test_install_creates_fresh_file(fake_home: Path, client: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["install", client])
    assert rc == 0
    cfg = _PATHS[client]()
    assert cfg.exists(), f"{cfg} should have been created"
    entry = _read_rlaif_entry(client, cfg)
    command, args = _entry_command_args(client, entry)
    assert command == "rlaif"
    assert args == ["serve"]
    for key, value in EXTRA_ENTRY_KEYS.get(client, {}).items():
        assert entry[key] == value
    out = capsys.readouterr().out
    assert "installed" in out


def test_install_idempotent(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["install", "cursor"])
    capsys.readouterr()
    rc = main(["install", "cursor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already installed" in out


def test_install_refuses_conflict(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"rlaif": {"command": "different", "args": []}}}))
    rc = main(["install", "cursor"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "different" in err.lower()
    assert "--force" in err
    # File must be unchanged.
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["rlaif"]["command"] == "different"


def test_install_force_overrides_conflict(fake_home: Path) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"rlaif": {"command": "different", "args": []}}}))
    rc = main(["install", "cursor", "--force"])
    assert rc == 0
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["rlaif"]["command"] == "rlaif"


def test_install_preserves_other_servers(fake_home: Path) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x", "args": ["y"]}}, "unrelated": 42}))
    rc = main(["install", "cursor"])
    assert rc == 0
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x", "args": ["y"]}
    assert "rlaif" in data["mcpServers"]
    assert data["unrelated"] == 42


def test_install_creates_backup_on_existing_file(fake_home: Path) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"x": {"command": "y", "args": []}}}))
    main(["install", "cursor"])
    bak = cfg.with_name(cfg.name + ".rlaif.bak")
    assert bak.exists()
    bak_data = json.loads(bak.read_text())
    assert "rlaif" not in bak_data.get("mcpServers", {})
    assert "x" in bak_data["mcpServers"]


def test_install_no_backup_on_fresh_file(fake_home: Path) -> None:
    rc = main(["install", "cursor"])
    assert rc == 0
    cfg = _PATHS["cursor"]()
    bak = cfg.with_name(cfg.name + ".rlaif.bak")
    assert not bak.exists()


def test_install_dev_path(fake_home: Path, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    rc = main(["install", "cursor", "--dev-path", str(src)])
    assert rc == 0
    data = json.loads(_PATHS["cursor"]().read_text())
    entry = data["mcpServers"]["rlaif"]
    assert entry["command"] == "uv"
    assert "--directory" in entry["args"]
    assert str(src.resolve()) in entry["args"]


def test_install_dry_run_does_not_write(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["install", "cursor", "--dry-run"])
    assert rc == 0
    cfg = _PATHS["cursor"]()
    assert not cfg.exists()
    out = capsys.readouterr().out
    assert "would write" in out
    assert '"rlaif"' in out


def test_install_rejects_invalid_json_file(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{not valid json")
    rc = main(["install", "cursor"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not valid JSON" in err


def test_install_rejects_non_object_mcpservers(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": "not a dict"}))
    rc = main(["install", "cursor"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "non-object" in err


# ---------------------------------------------------------------------------
# install — unsupported clients redirect to snippet
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client", list(TRULY_UNSUPPORTED))
def test_install_unsupported_redirects_to_snippet(
    fake_home: Path, client: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["install", client])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not supported" in err
    assert f"rlaif snippet {client}" in err


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def test_uninstall_removes_only_rlaif_entry(fake_home: Path) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "rlaif": {"command": "rlaif", "args": ["serve"]},
                    "keep": {"command": "x", "args": []},
                }
            }
        )
    )
    rc = main(["uninstall", "cursor"])
    assert rc == 0
    data = json.loads(cfg.read_text())
    assert "rlaif" not in data["mcpServers"]
    assert "keep" in data["mcpServers"]


def test_uninstall_when_not_present(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x", "args": []}}}))
    rc = main(["uninstall", "cursor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not installed" in out
    # File must be unchanged.
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"]["command"] == "x"


def test_uninstall_when_file_missing(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["uninstall", "cursor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "does not exist" in out


def test_uninstall_creates_backup(fake_home: Path) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"rlaif": {"command": "rlaif", "args": ["serve"]}}}))
    main(["uninstall", "cursor"])
    bak = cfg.with_name(cfg.name + ".rlaif.bak")
    assert bak.exists()


def test_uninstall_dry_run_does_not_write(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"rlaif": {"command": "rlaif", "args": ["serve"]}}}))
    rc = main(["uninstall", "cursor", "--dry-run"])
    assert rc == 0
    # Confirm file wasn't actually mutated.
    data = json.loads(cfg.read_text())
    assert "rlaif" in data["mcpServers"]
    out = capsys.readouterr().out
    assert "would write" in out


@pytest.mark.parametrize("client", list(TRULY_UNSUPPORTED))
def test_uninstall_unsupported_directs_to_manual(
    fake_home: Path, client: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["uninstall", client])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not supported" in err
    assert "manually" in err


# ---------------------------------------------------------------------------
# atomicity / mode preservation
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")
def test_install_preserves_existing_mode(fake_home: Path) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {}}))
    cfg.chmod(0o600)
    main(["install", "cursor"])
    assert (cfg.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# codex (TOML) — round-trip via tomlkit
# ---------------------------------------------------------------------------


def test_install_codex_idempotent(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["install", "codex"])
    capsys.readouterr()
    rc = main(["install", "codex"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already installed" in out


def test_install_codex_preserves_comments_and_siblings(fake_home: Path) -> None:
    cfg = _PATHS["codex"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "# user-managed file — keep this comment\n"
        'model = "gpt-5"\n'
        "\n"
        "[mcp_servers.other]\n"
        "# inline reasoning for `other`\n"
        'command = "other"\n'
        'args = ["x", "y"]\n'
    )
    rc = main(["install", "codex"])
    assert rc == 0
    text = cfg.read_text()
    assert "# user-managed file — keep this comment" in text
    assert "# inline reasoning for `other`" in text
    assert 'model = "gpt-5"' in text
    # Both servers present after install.
    doc = _load_toml(cfg)
    servers = doc["mcp_servers"]
    assert "other" in servers
    assert "rlaif" in servers
    assert servers["other"]["command"] == "other"
    assert list(servers["other"]["args"]) == ["x", "y"]
    assert servers["rlaif"]["command"] == "rlaif"
    assert list(servers["rlaif"]["args"]) == ["serve"]


def test_install_codex_refuses_conflict(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["codex"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[mcp_servers.rlaif]\ncommand = "different"\nargs = []\n')
    rc = main(["install", "codex"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "different" in err.lower()
    assert "--force" in err
    # File untouched.
    assert 'command = "different"' in cfg.read_text()


def test_install_codex_force_overrides(fake_home: Path) -> None:
    cfg = _PATHS["codex"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[mcp_servers.rlaif]\ncommand = "different"\nargs = []\n')
    rc = main(["install", "codex", "--force"])
    assert rc == 0
    doc = _load_toml(cfg)
    assert doc["mcp_servers"]["rlaif"]["command"] == "rlaif"


def test_install_codex_rejects_invalid_toml(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["codex"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text("not = valid = toml\n")
    rc = main(["install", "codex"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not valid TOML" in err


def test_uninstall_codex_keeps_siblings_and_comments(fake_home: Path) -> None:
    cfg = _PATHS["codex"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "# preserve me\n"
        'model = "gpt-5"\n'
        "\n"
        "[mcp_servers.other]\n"
        'command = "other"\n'
        "args = []\n"
        "\n"
        "[mcp_servers.rlaif]\n"
        'command = "rlaif"\n'
        'args = ["serve"]\n'
    )
    rc = main(["uninstall", "codex"])
    assert rc == 0
    text = cfg.read_text()
    assert "# preserve me" in text
    assert 'model = "gpt-5"' in text
    doc = _load_toml(cfg)
    servers = doc["mcp_servers"]
    assert "other" in servers
    assert "rlaif" not in servers


# ---------------------------------------------------------------------------
# hermes (YAML) — round-trip via ruamel.yaml
# ---------------------------------------------------------------------------


def test_install_hermes_writes_full_tools_block(fake_home: Path) -> None:
    rc = main(["install", "hermes"])
    assert rc == 0
    cfg = _PATHS["hermes"]()
    doc = _load_yaml(cfg)
    rlaif = doc["mcp_servers"]["rlaif"]
    assert rlaif["command"] == "rlaif"
    assert list(rlaif["args"]) == ["serve"]
    tools = rlaif["tools"]
    assert list(tools["include"]) == ["rlaif_info", "rlaif_log", "rlaif"]
    assert tools["prompts"] is False
    assert tools["resources"] is False


def test_install_hermes_idempotent(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["install", "hermes"])
    capsys.readouterr()
    rc = main(["install", "hermes"])
    assert rc == 0
    assert "already installed" in capsys.readouterr().out


def test_install_hermes_preserves_comments_and_siblings(fake_home: Path) -> None:
    cfg = _PATHS["hermes"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "# top-level comment, keep me\n"
        "log_level: debug\n"
        "mcp_servers:\n"
        "  other:\n"
        "    # inline comment on other\n"
        "    command: other\n"
        "    args: [a, b]\n"
    )
    rc = main(["install", "hermes"])
    assert rc == 0
    text = cfg.read_text()
    assert "# top-level comment, keep me" in text
    assert "# inline comment on other" in text
    assert "log_level: debug" in text
    doc = _load_yaml(cfg)
    servers = doc["mcp_servers"]
    assert "other" in servers
    assert "rlaif" in servers
    assert servers["other"]["command"] == "other"
    assert list(servers["other"]["args"]) == ["a", "b"]


def test_install_hermes_refuses_conflict(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["hermes"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text("mcp_servers:\n  rlaif:\n    command: different\n    args: []\n")
    rc = main(["install", "hermes"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "different" in err.lower()
    assert "--force" in err
    assert "command: different" in cfg.read_text()


def test_install_hermes_force_overrides(fake_home: Path) -> None:
    cfg = _PATHS["hermes"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text("mcp_servers:\n  rlaif:\n    command: different\n    args: []\n")
    rc = main(["install", "hermes", "--force"])
    assert rc == 0
    doc = _load_yaml(cfg)
    assert doc["mcp_servers"]["rlaif"]["command"] == "rlaif"
    # Tools block was added on overwrite.
    assert "tools" in doc["mcp_servers"]["rlaif"]


def test_install_hermes_rejects_invalid_yaml(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["hermes"]()
    cfg.parent.mkdir(parents=True)
    # Tab indentation in a block mapping is a YAML parse error.
    cfg.write_text("mcp_servers:\n\trlaif: oops\n")
    rc = main(["install", "hermes"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not valid YAML" in err


# ---------------------------------------------------------------------------
# opencode (JSON, custom schema)
# ---------------------------------------------------------------------------


def test_install_opencode_writes_custom_schema(fake_home: Path) -> None:
    rc = main(["install", "opencode"])
    assert rc == 0
    cfg = _PATHS["opencode"]()
    data = json.loads(cfg.read_text())
    # Fresh-file install seeds the $schema reference + the mcp.rlaif entry.
    assert data["$schema"] == "https://opencode.ai/config.json"
    rlaif = data["mcp"]["rlaif"]
    assert rlaif["type"] == "local"
    assert rlaif["command"] == ["rlaif", "serve"]
    assert rlaif["enabled"] is True
    # Nothing under "mcpServers" — opencode doesn't use that key.
    assert "mcpServers" not in data


def test_install_opencode_idempotent(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["install", "opencode"])
    capsys.readouterr()
    rc = main(["install", "opencode"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already installed" in out


def test_install_opencode_preserves_other_servers_and_top_keys(fake_home: Path) -> None:
    cfg = _PATHS["opencode"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "model": "claude-4-sonnet",
                "mcp": {"fs": {"type": "local", "command": ["fs", "serve"], "enabled": True}},
            }
        )
    )
    rc = main(["install", "opencode"])
    assert rc == 0
    data = json.loads(cfg.read_text())
    assert data["model"] == "claude-4-sonnet"
    assert "fs" in data["mcp"]
    assert data["mcp"]["fs"]["command"] == ["fs", "serve"]
    assert data["mcp"]["rlaif"]["command"] == ["rlaif", "serve"]


def test_install_opencode_refuses_conflict(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["opencode"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "mcp": {
                    "rlaif": {
                        "type": "local",
                        "command": ["different", "serve"],
                        "enabled": True,
                    }
                }
            }
        )
    )
    rc = main(["install", "opencode"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "different" in err.lower()
    assert "--force" in err
    # Untouched.
    data = json.loads(cfg.read_text())
    assert data["mcp"]["rlaif"]["command"] == ["different", "serve"]


def test_install_opencode_force_overrides_conflict(fake_home: Path) -> None:
    cfg = _PATHS["opencode"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "mcp": {
                    "rlaif": {
                        "type": "local",
                        "command": ["different", "serve"],
                        "enabled": True,
                    }
                }
            }
        )
    )
    rc = main(["install", "opencode", "--force"])
    assert rc == 0
    data = json.loads(cfg.read_text())
    assert data["mcp"]["rlaif"]["command"] == ["rlaif", "serve"]


def test_install_opencode_dev_path(fake_home: Path, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    rc = main(["install", "opencode", "--dev-path", str(src)])
    assert rc == 0
    data = json.loads(_PATHS["opencode"]().read_text())
    cmd = data["mcp"]["rlaif"]["command"]
    assert cmd[0] == "uv"
    assert "--directory" in cmd
    assert str(src.resolve()) in cmd


def test_install_opencode_jsonc_redirects_to_snippet(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["opencode"]()
    cfg.parent.mkdir(parents=True)
    # JSONC content — comments make the JSON parser fail, which we surface
    # as a hint at running `rlaif snippet opencode`.
    cfg.write_text('// commented config — opencode.jsonc shape\n{\n  "mcp": {}\n}\n')
    rc = main(["install", "opencode"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "rlaif snippet opencode" in err


def test_uninstall_opencode_keeps_other_servers(fake_home: Path) -> None:
    cfg = _PATHS["opencode"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {
                    "fs": {"type": "local", "command": ["fs", "serve"], "enabled": True},
                    "rlaif": {
                        "type": "local",
                        "command": ["rlaif", "serve"],
                        "enabled": True,
                    },
                },
            }
        )
    )
    rc = main(["uninstall", "opencode"])
    assert rc == 0
    data = json.loads(cfg.read_text())
    assert "rlaif" not in data["mcp"]
    assert "fs" in data["mcp"]
    assert data["$schema"] == "https://opencode.ai/config.json"


# ---------------------------------------------------------------------------
# hermes uninstall (kept inline below for grouping)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# autodetect — `rlaif install` / `rlaif uninstall` with no CLIENT
# ---------------------------------------------------------------------------


def test_install_autodetect_no_configs_prints_hint(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["install"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no MCP client configs detected" in err
    # Nothing was written.
    for client in SUPPORTED:
        assert not _PATHS[client]().exists(), f"{client} config should not have been created"


def test_install_autodetect_writes_only_existing_configs(fake_home: Path) -> None:
    # Pre-create cursor + codex configs only. Other clients should be left alone.
    cursor_cfg = _PATHS["cursor"]()
    cursor_cfg.parent.mkdir(parents=True)
    cursor_cfg.write_text(json.dumps({"mcpServers": {}}))

    codex_cfg = _PATHS["codex"]()
    codex_cfg.parent.mkdir(parents=True)
    codex_cfg.write_text("# user file\n")

    rc = main(["install"])
    assert rc == 0

    # The two we seeded got rlaif.
    assert _read_rlaif_entry("cursor", cursor_cfg)["command"] == "rlaif"
    assert _read_rlaif_entry("codex", codex_cfg)["command"] == "rlaif"

    # Nothing else got written.
    untouched = [c for c in SUPPORTED if c not in {"cursor", "codex"}]
    for client in untouched:
        assert not _PATHS[client]().exists(), f"{client} config should not have been auto-created"


def test_install_autodetect_lists_detected_clients(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {}}))
    rc = main(["install"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "detected" in out
    assert "cursor" in out


def test_install_autodetect_passes_dev_path(fake_home: Path, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {}}))
    rc = main(["install", "--dev-path", str(src)])
    assert rc == 0
    entry = _read_rlaif_entry("cursor", cfg)
    assert entry["command"] == "uv"
    assert str(src.resolve()) in entry["args"]


def test_install_autodetect_dry_run_does_not_write(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {}}))
    rc = main(["install", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would write" in out
    # File still has only the empty mcpServers we seeded.
    data = json.loads(cfg.read_text())
    assert "rlaif" not in data["mcpServers"]


def test_install_autodetect_continues_past_per_client_conflict(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One conflicting client should not block the rest from installing."""
    # cursor has a different rlaif entry → conflict, exit 1 from that one.
    cursor_cfg = _PATHS["cursor"]()
    cursor_cfg.parent.mkdir(parents=True)
    cursor_cfg.write_text(json.dumps({"mcpServers": {"rlaif": {"command": "different", "args": []}}}))
    # codex is empty → clean install.
    codex_cfg = _PATHS["codex"]()
    codex_cfg.parent.mkdir(parents=True)
    codex_cfg.write_text("")

    rc = main(["install"])
    # Worst per-client exit code propagates.
    assert rc == 1
    captured = capsys.readouterr()
    # cursor refused.
    assert "different" in captured.err.lower()
    # codex still got installed.
    assert _read_rlaif_entry("codex", codex_cfg)["command"] == "rlaif"
    # cursor was not overwritten.
    assert json.loads(cursor_cfg.read_text())["mcpServers"]["rlaif"]["command"] == "different"


def test_install_autodetect_idempotent_on_second_run(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {}}))
    main(["install"])
    capsys.readouterr()
    rc = main(["install"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already installed" in out


def test_uninstall_autodetect_no_rlaif_anywhere_prints_hint(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # cursor exists but has no rlaif.
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x", "args": []}}}))
    rc = main(["uninstall"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "rlaif is not installed" in err
    # File untouched.
    assert "other" in json.loads(cfg.read_text())["mcpServers"]


def test_uninstall_autodetect_removes_only_clients_with_rlaif(fake_home: Path) -> None:
    # cursor + kiro both have rlaif; antigravity exists but doesn't.
    for client in ("cursor", "kiro"):
        cfg = _PATHS[client]()
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "rlaif": {"command": "rlaif", "args": ["serve"]},
                        "keep": {"command": "x", "args": []},
                    }
                }
            )
        )
    antigravity_cfg = _PATHS["antigravity"]()
    antigravity_cfg.parent.mkdir(parents=True)
    antigravity_cfg.write_text(json.dumps({"mcpServers": {"unrelated": {"command": "y", "args": []}}}))

    rc = main(["uninstall"])
    assert rc == 0

    for client in ("cursor", "kiro"):
        data = json.loads(_PATHS[client]().read_text())
        assert "rlaif" not in data["mcpServers"]
        assert "keep" in data["mcpServers"]

    # antigravity was not opened-for-write — its single sibling key is intact.
    data = json.loads(antigravity_cfg.read_text())
    assert "rlaif" not in data["mcpServers"]
    assert "unrelated" in data["mcpServers"]


def test_uninstall_autodetect_dry_run_does_not_write(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"rlaif": {"command": "rlaif", "args": ["serve"]}}}))
    rc = main(["uninstall", "--dry-run"])
    assert rc == 0
    # File untouched.
    assert "rlaif" in json.loads(cfg.read_text())["mcpServers"]
    out = capsys.readouterr().out
    assert "would write" in out


def test_uninstall_hermes_keeps_siblings_and_comments(fake_home: Path) -> None:
    cfg = _PATHS["hermes"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "# keep me\n"
        "log_level: debug\n"
        "mcp_servers:\n"
        "  other:\n"
        "    command: other\n"
        "    args: []\n"
        "  rlaif:\n"
        "    command: rlaif\n"
        "    args: [serve]\n"
        "    tools:\n"
        "      include: [rlaif_info, rlaif_log, rlaif]\n"
        "      prompts: false\n"
        "      resources: false\n"
    )
    rc = main(["uninstall", "hermes"])
    assert rc == 0
    text = cfg.read_text()
    assert "# keep me" in text
    assert "log_level: debug" in text
    doc = _load_yaml(cfg)
    servers = doc["mcp_servers"]
    assert "other" in servers
    assert "rlaif" not in servers


# ---------------------------------------------------------------------------
# 3.0 client set — registry shape, config paths, windsurf removal
# ---------------------------------------------------------------------------


def test_registry_shape() -> None:
    assert set(SUPPORTED) == set(JSON_CLIENTS) | {"opencode", "codex", "grok", "hermes"}
    assert set(CLIENTS) - set(SUPPORTED) == set(TRULY_UNSUPPORTED)
    # Snippet-only clients sort last in `rlaif init`.
    assert CLIENTS[-2:] == TRULY_UNSUPPORTED
    assert len(set(CLIENTS)) == len(CLIENTS)


@pytest.mark.parametrize("client", sorted(HOME_RELATIVE_PATHS))
def test_client_config_path(fake_home: Path, client: str) -> None:
    assert _PATHS[client]() == fake_home.joinpath(*HOME_RELATIVE_PATHS[client])


@pytest.mark.parametrize("command", ["install", "uninstall", "snippet"])
def test_windsurf_is_an_unknown_client(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command, "windsurf"])
    assert exc.value.code == 2
    assert "windsurf" in capsys.readouterr().err
    assert "windsurf" not in CLIENTS_REGISTRY


def test_installer_rejects_windsurf_without_touching_legacy_config(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = fake_home / ".codeium" / "windsurf" / "mcp_config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"mcpServers": {}}))
    assert install("windsurf") == 2
    assert "not supported" in capsys.readouterr().err
    # Autodetect must not pick the old Windsurf file up either.
    assert main(["install"]) == 0
    assert "no MCP client configs detected" in capsys.readouterr().err
    assert json.loads(legacy.read_text()) == {"mcpServers": {}}


# ---------------------------------------------------------------------------
# round trips across the whole registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client", list(JSON_CLIENTS))
def test_json_client_round_trip_preserves_siblings(fake_home: Path, client: str) -> None:
    cfg = _PATHS[client]()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    original = {"mcpServers": {"keep": {"command": "x", "args": ["y"]}}, "unrelated": 42}
    cfg.write_text(json.dumps(original))
    assert main(["install", client]) == 0
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["keep"] == {"command": "x", "args": ["y"]}
    assert data["unrelated"] == 42
    assert "rlaif" in data["mcpServers"]
    assert main(["uninstall", client]) == 0
    assert json.loads(cfg.read_text()) == original


@pytest.mark.parametrize("client", list(SUPPORTED))
def test_install_then_uninstall_removes_entry(fake_home: Path, client: str) -> None:
    assert main(["install", client]) == 0
    assert main(["uninstall", client]) == 0
    cfg = _PATHS[client]()
    adapter = CLIENTS_REGISTRY[client].format_adapter
    assert adapter is not None
    assert adapter.get_rlaif(adapter.parse(cfg.read_text(), cfg)) is None


@pytest.mark.parametrize("client", list(JSON_CLIENTS))
def test_snippet_matches_installed_entry(fake_home: Path, client: str) -> None:
    snippet = json.loads(CLIENTS_REGISTRY[client].snippet_builder("rlaif", ["serve"]))
    assert main(["install", client]) == 0
    installed = _read_rlaif_entry(client, _PATHS[client]())
    assert snippet["mcpServers"]["rlaif"] == installed
    assert list(snippet["mcpServers"]["rlaif"]) == list(installed)


def test_install_autodetect_covers_new_clients(fake_home: Path) -> None:
    new_clients = ("antigravity", "devin", "kiro", "junie", "lmstudio", "copilot", "droid", "grok")
    for client in new_clients:
        cfg = _PATHS[client]()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("# user file\n" if client == "grok" else "{}")
    assert main(["install"]) == 0
    for client in new_clients:
        assert _entry_command_args(client, _read_rlaif_entry(client, _PATHS[client]())) == ("rlaif", ["serve"])


# ---------------------------------------------------------------------------
# grok (TOML) — shares codex's adapter and shape
# ---------------------------------------------------------------------------


def test_install_grok_preserves_comments_and_siblings(fake_home: Path) -> None:
    cfg = _PATHS["grok"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '# grok settings, keep me\nmodel = "some-model"\n\n[mcp_servers.other]\ncommand = "other"\nargs = ["x"]\n'
    )
    assert main(["install", "grok"]) == 0
    text = cfg.read_text()
    assert "# grok settings, keep me" in text
    assert 'model = "some-model"' in text
    servers = _load_toml(cfg)["mcp_servers"]
    assert servers["other"]["command"] == "other"
    assert servers["rlaif"]["command"] == "rlaif"
    assert list(servers["rlaif"]["args"]) == ["serve"]
    assert main(["uninstall", "grok"]) == 0
    assert "# grok settings, keep me" in cfg.read_text()
    assert list(_load_toml(cfg)["mcp_servers"]) == ["other"]


def test_install_grok_refuses_conflict(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _PATHS["grok"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[mcp_servers.rlaif]\ncommand = "different"\nargs = []\n')
    assert main(["install", "grok"]) == 1
    assert "--force" in capsys.readouterr().err
    assert 'command = "different"' in cfg.read_text()


# ---------------------------------------------------------------------------
# copilot / droid — entries carry extra keys, and the conflict check expects them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client", sorted(EXTRA_ENTRY_KEYS))
def test_extra_key_clients_write_full_entry_and_are_idempotent(
    fake_home: Path, client: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["install", client]) == 0
    entry = _read_rlaif_entry(client, _PATHS[client]())
    assert entry == {"command": "rlaif", "args": ["serve"], **EXTRA_ENTRY_KEYS[client]}
    assert list(entry)[0] == "type"
    capsys.readouterr()
    assert main(["install", client]) == 0
    assert "already installed" in capsys.readouterr().out


def test_install_copilot_dry_run_shows_extra_keys(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["install", "copilot", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert '"type": "stdio"' in out
    assert '"tools"' in out
    assert not _PATHS["copilot"]().exists()


@pytest.mark.parametrize(
    ("client", "stale"),
    [
        # Missing `type` and/or `tools` must count as a conflict, not a match.
        ("copilot", {"command": "rlaif", "args": ["serve"]}),
        ("copilot", {"type": "stdio", "command": "rlaif", "args": ["serve"]}),
        ("copilot", {"command": "rlaif", "args": ["serve"], "tools": ["*"]}),
        ("copilot", {"type": "stdio", "command": "rlaif", "args": ["serve"], "tools": ["rlaif_info"]}),
        ("droid", {"command": "rlaif", "args": ["serve"]}),
    ],
)
def test_extra_key_clients_treat_incomplete_entry_as_conflict(
    fake_home: Path, client: str, stale: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _PATHS[client]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"rlaif": stale}}))
    assert main(["install", client]) == 1
    assert "--force" in capsys.readouterr().err
    assert json.loads(cfg.read_text())["mcpServers"]["rlaif"] == stale
    # --force repairs it to the full entry.
    assert main(["install", client, "--force"]) == 0
    entry = _read_rlaif_entry(client, cfg)
    assert entry == {"command": "rlaif", "args": ["serve"], **EXTRA_ENTRY_KEYS[client]}


def test_plain_json_client_rejects_entry_with_extra_keys(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # cursor's adapter must not inherit copilot's `extra`: a copilot-shaped
    # entry is a conflict there, and a plain install writes no extra keys.
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    stale = {"type": "stdio", "command": "rlaif", "args": ["serve"], "tools": ["*"]}
    cfg.write_text(json.dumps({"mcpServers": {"rlaif": stale}}))
    assert main(["install", "cursor"]) == 1
    assert "--force" in capsys.readouterr().err
    assert main(["install", "cursor", "--force"]) == 0
    assert _read_rlaif_entry("cursor", cfg) == {"command": "rlaif", "args": ["serve"]}


# ---------------------------------------------------------------------------
# env-var path overrides (COPILOT_HOME, XDG_CONFIG_HOME, APPDATA)
# ---------------------------------------------------------------------------


def test_copilot_home_override(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = fake_home / "elsewhere" / "copilot"
    monkeypatch.setenv("COPILOT_HOME", str(custom))
    assert copilot_path() == custom / "mcp-config.json"
    assert main(["install", "copilot"]) == 0
    assert (custom / "mcp-config.json").exists()
    assert not (fake_home / ".copilot").exists()
    assert main(["uninstall", "copilot"]) == 0
    assert "rlaif" not in json.loads((custom / "mcp-config.json").read_text())["mcpServers"]


def test_empty_copilot_home_falls_back_to_default(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COPILOT_HOME", "")
    assert copilot_path() == fake_home / ".copilot" / "mcp-config.json"


@pytest.mark.skipif(sys.platform == "win32", reason="XDG applies to macOS and Linux")
def test_devin_xdg_config_home_override(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = fake_home / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(custom))
    assert devin_path() == custom / "devin" / "mcp_config.json"
    assert main(["install", "devin"]) == 0
    assert (custom / "devin" / "mcp_config.json").exists()
    assert not (fake_home / ".config").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="XDG applies to macOS and Linux")
def test_devin_ignores_relative_xdg_config_home(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert devin_path() == fake_home / ".config" / "devin" / "mcp_config.json"


def test_devin_windows_uses_appdata(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / "xdg"))
    appdata = fake_home / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    assert devin_path() == appdata / "devin" / "mcp_config.json"
    monkeypatch.delenv("APPDATA")
    assert devin_path() == fake_home / "AppData" / "Roaming" / "devin" / "mcp_config.json"

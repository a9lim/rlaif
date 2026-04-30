"""Tests for `rlaif install` / `rlaif uninstall` (7 supported clients).

Five JSON clients (claude-desktop, claude-code, cursor, windsurf,
antigravity) plus codex (TOML, via tomlkit) and hermes (YAML, via
ruamel.yaml). All tests redirect ``Path.home()`` (and on macOS the
claude-desktop sub-path) into a tmp dir so they never touch real user
state.
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

from rlaif._clients import CLIENTS_REGISTRY
from rlaif._clients import INSTALL_SUPPORTED as SUPPORTED
from rlaif.cli import main


# Tests pre-refactor used a ``_PATHS[client]()`` dict keyed on client name.
# Post-refactor the path resolvers live on each ``ClientRecord``; this
# tiny shim preserves the call-site shape without churn across the file.
class _PathLookup:
    def __getitem__(self, client: str) -> Callable[[], Path]:
        record = CLIENTS_REGISTRY[client]
        assert record.path_fn is not None, f"{client} is snippet-only"
        return record.path_fn


_PATHS = _PathLookup()

JSON_CLIENTS = (
    "claude-desktop",
    "claude-code",
    "cursor",
    "windsurf",
    "antigravity",
)
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
    if client == "codex":
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
    # On Windows the claude-desktop path also reads $APPDATA. We don't run
    # tests on Windows in CI, but be defensive.
    monkeypatch.delenv("APPDATA", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# install — supported clients
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client", list(SUPPORTED))
def test_install_creates_fresh_file(
    fake_home: Path, client: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["install", client])
    assert rc == 0
    cfg = _PATHS[client]()
    assert cfg.exists(), f"{cfg} should have been created"
    entry = _read_rlaif_entry(client, cfg)
    command, args = _entry_command_args(client, entry)
    assert command == "rlaif"
    assert args == ["serve"]
    out = capsys.readouterr().out
    assert "installed" in out


def test_install_idempotent(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["install", "cursor"])
    capsys.readouterr()
    rc = main(["install", "cursor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already installed" in out


def test_install_refuses_conflict(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps({"mcpServers": {"rlaif": {"command": "different", "args": []}}})
    )
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
    cfg.write_text(
        json.dumps({"mcpServers": {"rlaif": {"command": "different", "args": []}}})
    )
    rc = main(["install", "cursor", "--force"])
    assert rc == 0
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["rlaif"]["command"] == "rlaif"


def test_install_preserves_other_servers(fake_home: Path) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {"mcpServers": {"other": {"command": "x", "args": ["y"]}}, "unrelated": 42}
        )
    )
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


def test_install_dry_run_does_not_write(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["install", "cursor", "--dry-run"])
    assert rc == 0
    cfg = _PATHS["cursor"]()
    assert not cfg.exists()
    out = capsys.readouterr().out
    assert "would write" in out
    assert '"rlaif"' in out


def test_install_rejects_invalid_json_file(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{not valid json")
    rc = main(["install", "cursor"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not valid JSON" in err


def test_install_rejects_non_object_mcpservers(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_uninstall_when_not_present(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_uninstall_when_file_missing(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["uninstall", "cursor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "does not exist" in out


def test_uninstall_creates_backup(fake_home: Path) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps({"mcpServers": {"rlaif": {"command": "rlaif", "args": ["serve"]}}})
    )
    main(["uninstall", "cursor"])
    bak = cfg.with_name(cfg.name + ".rlaif.bak")
    assert bak.exists()


def test_uninstall_dry_run_does_not_write(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _PATHS["cursor"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps({"mcpServers": {"rlaif": {"command": "rlaif", "args": ["serve"]}}})
    )
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


def test_install_codex_idempotent(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
        '# user-managed file — keep this comment\n'
        'model = "gpt-5"\n'
        '\n'
        '[mcp_servers.other]\n'
        '# inline reasoning for `other`\n'
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


def test_install_codex_refuses_conflict(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _PATHS["codex"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '[mcp_servers.rlaif]\n'
        'command = "different"\n'
        'args = []\n'
    )
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
    cfg.write_text(
        '[mcp_servers.rlaif]\n'
        'command = "different"\n'
        'args = []\n'
    )
    rc = main(["install", "codex", "--force"])
    assert rc == 0
    doc = _load_toml(cfg)
    assert doc["mcp_servers"]["rlaif"]["command"] == "rlaif"


def test_install_codex_rejects_invalid_toml(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
        '# preserve me\n'
        'model = "gpt-5"\n'
        '\n'
        '[mcp_servers.other]\n'
        'command = "other"\n'
        'args = []\n'
        '\n'
        '[mcp_servers.rlaif]\n'
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


def test_install_hermes_idempotent(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_install_hermes_refuses_conflict(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _PATHS["hermes"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "mcp_servers:\n"
        "  rlaif:\n"
        "    command: different\n"
        "    args: []\n"
    )
    rc = main(["install", "hermes"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "different" in err.lower()
    assert "--force" in err
    assert "command: different" in cfg.read_text()


def test_install_hermes_force_overrides(fake_home: Path) -> None:
    cfg = _PATHS["hermes"]()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "mcp_servers:\n"
        "  rlaif:\n"
        "    command: different\n"
        "    args: []\n"
    )
    rc = main(["install", "hermes", "--force"])
    assert rc == 0
    doc = _load_yaml(cfg)
    assert doc["mcp_servers"]["rlaif"]["command"] == "rlaif"
    # Tools block was added on overwrite.
    assert "tools" in doc["mcp_servers"]["rlaif"]


def test_install_hermes_rejects_invalid_yaml(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_install_opencode_idempotent(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
                "mcp": {
                    "fs": {"type": "local", "command": ["fs", "serve"], "enabled": True}
                },
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


def test_install_opencode_refuses_conflict(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_install_opencode_jsonc_redirects_to_snippet(
    fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _PATHS["opencode"]()
    cfg.parent.mkdir(parents=True)
    # JSONC content — comments make the JSON parser fail, which we surface
    # as a hint at running `rlaif snippet opencode`.
    cfg.write_text(
        '// commented config — opencode.jsonc shape\n'
        '{\n  "mcp": {}\n}\n'
    )
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

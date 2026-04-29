"""Tests for `rlaif install` / `rlaif uninstall` (phase 1: 5 supported clients).

All tests redirect ``Path.home()`` (and on macOS the claude-desktop sub-path)
into a tmp dir so they never touch real user state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from rlaif.cli import main
from rlaif.installer import _PATHS, SUPPORTED


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
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["rlaif"] == {"command": "rlaif", "args": ["serve"]}
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


@pytest.mark.parametrize("client", ["codex", "hermes", "opencode", "vscode", "zed"])
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


@pytest.mark.parametrize("client", ["codex", "hermes", "opencode", "vscode", "zed"])
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

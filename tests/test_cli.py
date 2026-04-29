"""CLI dispatcher tests. Verifies argument routing and snippet output shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from rlaif.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "rlaif" in out


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    for cmd in ("serve", "init", "doctor", "dry-run", "live-smoke", "log", "snippet"):
        assert cmd in out


def test_unknown_subcommand_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["not-a-real-subcommand"])


def test_snippet_claude_desktop(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["snippet", "claude-desktop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcpServers" in out
    assert '"rlaif"' in out


def test_snippet_claude_code(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["snippet", "claude-code"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcpServers" in out


def test_snippet_codex(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["snippet", "codex", "--dev-path", "/tmp/rlaif"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[mcp_servers.rlaif]" in out
    assert "/tmp/rlaif" in out
    assert "--directory" in out


def test_snippet_hermes(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["snippet", "hermes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcp_servers:" in out
    assert "rlaif_info" in out
    assert "include:" in out


def test_snippet_antigravity(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["snippet", "antigravity"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcpServers" in out
    assert '"rlaif"' in out
    assert "~/.gemini/antigravity/mcp_config.json" in out


def test_snippet_opencode(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["snippet", "opencode"])
    assert rc == 0
    out = capsys.readouterr().out
    # opencode-specific schema markers
    assert '"mcp"' in out
    assert '"type": "local"' in out
    assert '"$schema"' in out
    # `command` is an array (command + args merged), not a string + separate args
    assert '"command": [' in out
    assert "mcpServers" not in out


def test_snippet_opencode_dev_path(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rc = main(["snippet", "opencode", "--dev-path", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"uv"' in out
    assert "--directory" in out
    assert str(tmp_path.resolve()) in out


def test_snippet_cursor(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["snippet", "cursor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcpServers" in out
    assert '"rlaif"' in out
    assert "~/.cursor/mcp.json" in out


def test_snippet_windsurf(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["snippet", "windsurf"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcpServers" in out
    assert '"rlaif"' in out
    assert "~/.codeium/windsurf/mcp_config.json" in out


def test_snippet_vscode(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["snippet", "vscode"])
    assert rc == 0
    out = capsys.readouterr().out
    # vscode-specific schema: top-level `servers`, each entry needs type stdio
    assert '"servers"' in out
    assert '"type": "stdio"' in out
    assert "mcpServers" not in out
    assert ".vscode/mcp.json" in out


def test_snippet_zed(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["snippet", "zed"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"context_servers"' in out
    assert '"source": "custom"' in out
    assert "mcpServers" not in out
    assert "settings.json" in out


def test_snippet_dev_path_resolves(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rc = main(["snippet", "claude-desktop", "--dev-path", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(tmp_path.resolve()) in out
    assert '"uv"' in out


# ---------------------------------------------------------------------------
# rlaif log — tails the on-disk ops log
# ---------------------------------------------------------------------------


def test_log_no_file(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    rc = main(["log"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no ops log" in err


def test_log_tails_entries(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    log_dir = tmp_path / "rlaif"
    log_dir.mkdir()
    log_file = log_dir / "ops.jsonl"
    log_file.write_text(
        '{"op_id":"a"}\n{"op_id":"b"}\n{"op_id":"c"}\n', encoding="utf-8"
    )
    rc = main(["log", "--tail", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"b"' in out
    assert '"c"' in out
    assert '"a"' not in out


def test_log_raw_prints_lines_verbatim(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    log_dir = tmp_path / "rlaif"
    log_dir.mkdir()
    log_file = log_dir / "ops.jsonl"
    log_file.write_text('{"op_id":"z"}\n', encoding="utf-8")
    rc = main(["log", "--raw"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == '{"op_id":"z"}'


def test_log_tail_zero_shows_all(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    log_dir = tmp_path / "rlaif"
    log_dir.mkdir()
    log_file = log_dir / "ops.jsonl"
    log_file.write_text(
        '{"op_id":"1"}\n{"op_id":"2"}\n{"op_id":"3"}\n', encoding="utf-8"
    )
    rc = main(["log", "--tail", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"1"' in out
    assert '"2"' in out
    assert '"3"' in out

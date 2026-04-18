"""CLI dispatcher tests. Verifies argument routing and snippet output shape."""

from __future__ import annotations

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
    for cmd in ("serve", "init", "doctor", "dry-run", "live-smoke", "snippet"):
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


def test_snippet_dev_path_resolves(capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    rc = main(["snippet", "claude-desktop", "--dev-path", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(tmp_path.resolve()) in out
    assert '"uv"' in out

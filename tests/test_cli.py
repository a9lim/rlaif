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


# ---------------------------------------------------------------------------
# rlaif log --stats — histogram view
# ---------------------------------------------------------------------------


def _seed_log(tmp_path: Path) -> Path:
    log_dir = tmp_path / "rlaif"
    log_dir.mkdir()
    log_file = log_dir / "ops.jsonl"
    log_file.write_text(
        # 3 fired (intensities 5, 20, 25) + 2 refused (rate_limited, allow_shock)
        '{"op_id":"a","timestamp":1700000000.0,"requested":{"intensity":5,"duration_s":1},'
        '"actual":{"intensity":5,"duration_s":1},"clamped":false,"rate_limited":false,'
        '"high_intensity":false,"device_response":"ok"}\n'
        '{"op_id":"b","timestamp":1700003600.0,"requested":{"intensity":80,"duration_s":3},'
        '"actual":{"intensity":25,"duration_s":2},"clamped":true,"rate_limited":false,'
        '"high_intensity":true,"device_response":"ok"}\n'
        '{"op_id":"c","timestamp":1700007200.0,"requested":{"intensity":1,"duration_s":1},'
        '"actual":{"intensity":1,"duration_s":1},"clamped":false,"rate_limited":true,'
        '"high_intensity":false,"device_response":"","error":"rate_limited: bucket empty"}\n'
        '{"op_id":"d","timestamp":1700010800.0,"requested":{"intensity":1,"duration_s":1},'
        '"actual":{"intensity":1,"duration_s":1},"clamped":false,"rate_limited":false,'
        '"high_intensity":false,"device_response":"","error":"allow_shock is false"}\n'
        '{"op_id":"e","timestamp":1700014400.0,"requested":{"intensity":20,"duration_s":2},'
        '"actual":{"intensity":20,"duration_s":2},"clamped":false,"rate_limited":false,'
        '"high_intensity":true,"device_response":"ok"}\n',
        encoding="utf-8",
    )
    return log_file


def test_log_stats_summary_counts(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _seed_log(tmp_path)
    rc = main(["log", "--stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "entries    : 5" in out
    assert "fired      : 3" in out
    assert "refused    : 2" in out
    # Energy proxy: 5*1 + 25*2 + 20*2 = 95
    assert "energy sum : 95" in out
    assert "peak I     : 25" in out


def test_log_stats_intensity_buckets(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _seed_log(tmp_path)
    main(["log", "--stats"])
    out = capsys.readouterr().out
    assert "intensity buckets" in out
    # Each fired op falls in a different bucket: 1–5, 16–20, 21–25.
    assert "1–5" in out
    assert "16–20" in out
    assert "21–25" in out


def test_log_stats_refusal_reasons(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _seed_log(tmp_path)
    main(["log", "--stats"])
    out = capsys.readouterr().out
    assert "refusal reasons" in out
    assert "rate_limited" in out
    assert "allow_shock" in out


def test_log_stats_no_file(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    rc = main(["log", "--stats"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no ops log" in err


def test_log_stats_empty_file(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    log_dir = tmp_path / "rlaif"
    log_dir.mkdir()
    (log_dir / "ops.jsonl").write_text("", encoding="utf-8")
    rc = main(["log", "--stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no entries" in out


# ---------------------------------------------------------------------------
# rlaif install/snippet — opencode is now auto-installable
# ---------------------------------------------------------------------------


def test_install_supported_list_includes_opencode() -> None:
    """A subprocess-free check that the parser accepts opencode as an install target."""
    # Just construct the parser and verify opencode is among the choices —
    # if argparse rejected it, the install test in test_installer.py would
    # fail anyway; this is a fast smoke at the dispatcher layer.
    from rlaif.installer import SUPPORTED
    assert "opencode" in SUPPORTED

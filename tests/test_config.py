"""Config loader tests. Validation of safety fields is the safety spec's job;
these tests just verify loading, env overrides, and error-path quality."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from rlaif.config import Config, ConfigError, load


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_minimal_config(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        api_key = "k"
        sharecode = "ABCD"
        """,
    )
    cfg = load(p, env={})
    assert isinstance(cfg, Config)
    assert cfg.auth.username == "u"
    assert cfg.auth.api_key == "k"
    assert cfg.auth.sharecode == "ABCD"
    # Safety defaults from SafetyConfig
    assert cfg.safety.allow_shock is False
    assert cfg.safety.max_intensity == 25
    assert cfg.device.label == "device"


def test_env_overrides_file(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "file_user"
        api_key = "file_key"
        sharecode = "file_code"
        """,
    )
    cfg = load(
        p,
        env={
            "RLAIF_USERNAME": "env_user",
            "RLAIF_API_KEY": "env_key",
            "RLAIF_SHARECODE": "env_code",
        },
    )
    assert cfg.auth.username == "env_user"
    assert cfg.auth.api_key == "env_key"
    assert cfg.auth.sharecode == "env_code"


def test_missing_auth_reports_fields(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        """,
    )
    with pytest.raises(ConfigError, match="api_key"):
        load(p, env={})


def test_safety_gate_surfaces_via_config_error(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        api_key = "k"
        sharecode = "s"

        [safety]
        max_intensity = 40
        """,
    )
    with pytest.raises(ConfigError, match="i_understand_and_consent"):
        load(p, env={})


def test_consent_allows_raised_caps(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        api_key = "k"
        sharecode = "s"

        [safety]
        allow_shock = true
        max_intensity = 40
        i_understand_and_consent = true

        [rate_limit]
        bucket_capacity = 5
        """,
    )
    cfg = load(p, env={})
    assert cfg.safety.max_intensity == 40
    assert cfg.safety.bucket_capacity == 5


def test_refill_seconds_floor_enforced(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        api_key = "k"
        sharecode = "s"

        [rate_limit]
        refill_seconds = 10
        """,
    )
    with pytest.raises(ConfigError, match="refill_seconds"):
        load(p, env={})


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.toml", env={})


def test_invalid_toml(tmp_path: Path) -> None:
    p = tmp_path / "bad.toml"
    p.write_text("[auth\nnope", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load(p, env={})


def test_type_errors_are_actionable(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        api_key = "k"
        sharecode = "s"

        [safety]
        allow_shock = "true"
        """,
    )
    with pytest.raises(ConfigError, match="allow_shock"):
        load(p, env={})


def test_redacted_output_hides_api_key(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        api_key = "super-secret"
        sharecode = "ABCDEFGH"
        """,
    )
    cfg = load(p, env={})
    red = cfg.redacted()
    assert "super-secret" not in str(red)
    assert red["auth"]["api_key"] == "***redacted***"

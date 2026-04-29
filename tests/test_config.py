"""Config loader tests. Validation of safety fields is the safety spec's job;
these tests verify loading, env overrides, error-path quality, and the
provider abstraction (PiShock + OpenShock + legacy [auth] back-compat).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from rlaif.config import Config, ConfigError, load


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Legacy [auth] shape — still accepted, implies provider.kind = "pishock"
# ---------------------------------------------------------------------------


def test_legacy_auth_section(tmp_path: Path) -> None:
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
    assert cfg.provider.kind == "pishock"
    assert cfg.provider.raw["username"] == "u"
    assert cfg.provider.raw["api_key"] == "k"
    assert cfg.provider.raw["sharecode"] == "ABCD"
    assert cfg.safety.allow_shock is False
    assert cfg.safety.max_intensity == 25
    assert cfg.device.label == "device"


def test_legacy_env_overrides_file(tmp_path: Path) -> None:
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
    assert cfg.provider.kind == "pishock"
    assert cfg.provider.raw["username"] == "env_user"
    assert cfg.provider.raw["api_key"] == "env_key"
    assert cfg.provider.raw["sharecode"] == "env_code"


def test_missing_pishock_fields_reports_them(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        """,
    )
    with pytest.raises(ConfigError, match="api_key"):
        load(p, env={})


# ---------------------------------------------------------------------------
# New [provider] shape — pishock kind
# ---------------------------------------------------------------------------


def test_provider_pishock_explicit(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [provider]
        kind = "pishock"

        [provider.pishock]
        username = "u"
        api_key = "k"
        sharecode = "S1"
        """,
    )
    cfg = load(p, env={})
    assert cfg.provider.kind == "pishock"
    assert cfg.provider.raw["username"] == "u"
    assert cfg.provider.raw["sharecode"] == "S1"


def test_provider_pishock_env_overrides(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [provider]
        kind = "pishock"

        [provider.pishock]
        username = "file_user"
        api_key = "file_key"
        sharecode = "file_code"
        """,
    )
    cfg = load(p, env={"RLAIF_API_KEY": "env_key"})
    assert cfg.provider.raw["username"] == "file_user"
    assert cfg.provider.raw["api_key"] == "env_key"
    assert cfg.provider.raw["sharecode"] == "file_code"


# ---------------------------------------------------------------------------
# [provider] openshock
# ---------------------------------------------------------------------------


def test_provider_openshock_minimal(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [provider]
        kind = "openshock"

        [provider.openshock]
        api_token = "T"
        shocker_id = "abc"
        """,
    )
    cfg = load(p, env={})
    assert cfg.provider.kind == "openshock"
    assert cfg.provider.raw["api_token"] == "T"
    assert cfg.provider.raw["shocker_id"] == "abc"
    # base_url not provided — provider defaults to api.openshock.app at construct time.
    assert "base_url" not in cfg.provider.raw


def test_provider_openshock_with_base_url(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [provider]
        kind = "openshock"

        [provider.openshock]
        api_token = "T"
        shocker_id = "abc"
        base_url = "https://shock.local"
        """,
    )
    cfg = load(p, env={})
    assert cfg.provider.raw["base_url"] == "https://shock.local"


def test_provider_openshock_env_overrides(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [provider]
        kind = "openshock"

        [provider.openshock]
        api_token = "file_token"
        shocker_id = "file_shocker"
        """,
    )
    cfg = load(
        p,
        env={
            "RLAIF_OPENSHOCK_TOKEN": "env_token",
            "RLAIF_OPENSHOCK_SHOCKER_ID": "env_shocker",
            "RLAIF_OPENSHOCK_BASE_URL": "https://env.local",
        },
    )
    assert cfg.provider.raw["api_token"] == "env_token"
    assert cfg.provider.raw["shocker_id"] == "env_shocker"
    assert cfg.provider.raw["base_url"] == "https://env.local"


def test_provider_openshock_missing_fields(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [provider]
        kind = "openshock"

        [provider.openshock]
        api_token = "T"
        """,
    )
    with pytest.raises(ConfigError, match="shocker_id"):
        load(p, env={})


def test_provider_unknown_kind_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [provider]
        kind = "bogus"
        """,
    )
    with pytest.raises(ConfigError, match="bogus"):
        load(p, env={})


def test_provider_and_auth_both_present_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        api_key = "k"
        sharecode = "s"

        [provider]
        kind = "openshock"

        [provider.openshock]
        api_token = "T"
        shocker_id = "abc"
        """,
    )
    with pytest.raises(ConfigError, match="\\[auth\\]"):
        load(p, env={})


# ---------------------------------------------------------------------------
# Tool config — purpose preamble
# ---------------------------------------------------------------------------


def test_tool_purpose_loads(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        api_key = "k"
        sharecode = "s"

        [tool]
        purpose = "shock me when i open twitter during pomodoros"
        """,
    )
    cfg = load(p, env={})
    assert cfg.tool.purpose == "shock me when i open twitter during pomodoros"


def test_tool_purpose_blank_becomes_none(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        api_key = "k"
        sharecode = "s"

        [tool]
        purpose = "   "
        """,
    )
    cfg = load(p, env={})
    assert cfg.tool.purpose is None


def test_tool_purpose_default_is_none(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [auth]
        username = "u"
        api_key = "k"
        sharecode = "s"
        """,
    )
    cfg = load(p, env={})
    assert cfg.tool.purpose is None


# ---------------------------------------------------------------------------
# Safety + general loader paths (kept from the original suite)
# ---------------------------------------------------------------------------


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


def test_redacted_output_hides_secrets(tmp_path: Path) -> None:
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
    flat = str(red)
    assert "super-secret" not in flat
    assert red["provider"]["api_key"] == "***redacted***"
    assert red["provider"]["sharecode"].startswith("ABCD")
    assert "EFGH" not in red["provider"]["sharecode"]


def test_redacted_openshock_hides_token(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [provider]
        kind = "openshock"

        [provider.openshock]
        api_token = "super-secret-token"
        shocker_id = "abc"
        base_url = "https://shock.local"
        """,
    )
    cfg = load(p, env={})
    red = cfg.redacted()
    flat = str(red)
    assert "super-secret-token" not in flat
    assert red["provider"]["api_token"] == "***redacted***"
    # Non-secret fields pass through.
    assert red["provider"]["shocker_id"] == "abc"
    assert red["provider"]["base_url"] == "https://shock.local"

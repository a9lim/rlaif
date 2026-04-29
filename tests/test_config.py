"""Config loader tests for the 2.0 ``[negative]`` / ``[positive]`` schema.

Validation of safety field ranges is the safety spec's job; these tests
exercise loading, env overrides, error-path quality, and the per-channel
provider abstraction. The 1.x ``[auth]`` / ``[provider]`` / ``[device]`` /
``[safety]`` / ``[rate_limit]`` / ``[tool]`` schemas are gone — a config
that uses any of them is rejected with a migration message.
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
# Negative channel — pishock + openshock kinds
# ---------------------------------------------------------------------------


def test_negative_pishock_minimal(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind  = "pishock"
        label = "collar"

        [negative.pishock]
        username  = "u"
        api_key   = "k"
        sharecode = "ABCD"
        """,
    )
    cfg = load(p, env={})
    assert isinstance(cfg, Config)
    assert cfg.positive is None
    assert cfg.negative is not None
    assert cfg.negative.kind == "pishock"
    assert cfg.negative.label == "collar"
    assert cfg.negative.raw["username"] == "u"
    assert cfg.negative.raw["api_key"] == "k"
    assert cfg.negative.raw["sharecode"] == "ABCD"
    # Safety defaults: allow=false, conservative ceilings.
    assert cfg.negative.safety.allow is False
    assert cfg.negative.safety.max_intensity == 25
    assert cfg.negative.safety.spec.name == "negative"


def test_negative_openshock(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "openshock"

        [negative.openshock]
        api_token  = "TOK"
        shocker_id = "abc-123"
        base_url   = "https://shock.example.test"
        """,
    )
    cfg = load(p, env={})
    assert cfg.negative is not None
    assert cfg.negative.kind == "openshock"
    assert cfg.negative.raw["api_token"] == "TOK"
    assert cfg.negative.raw["shocker_id"] == "abc-123"
    assert cfg.negative.raw["base_url"] == "https://shock.example.test"


def test_negative_unknown_kind_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "tens-unit"
        """,
    )
    with pytest.raises(ConfigError, match="supported kinds"):
        load(p, env={})


def test_negative_missing_kind_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        label = "collar"

        [negative.pishock]
        username  = "u"
        api_key   = "k"
        sharecode = "s"
        """,
    )
    with pytest.raises(ConfigError, match="kind is required"):
        load(p, env={})


def test_negative_missing_pishock_fields_actionable(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "pishock"

        [negative.pishock]
        username = "u"
        """,
    )
    with pytest.raises(ConfigError, match="api_key.*sharecode"):
        load(p, env={})


def test_negative_pishock_env_override(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "pishock"

        [negative.pishock]
        username  = "FILE_user"
        api_key   = "FILE_key"
        sharecode = "FILE_share"
        """,
    )
    cfg = load(
        p,
        env={
            "RLAIF_PISHOCK_USERNAME": "ENV_user",
            "RLAIF_PISHOCK_API_KEY": "ENV_key",
        },
    )
    assert cfg.negative is not None
    assert cfg.negative.raw["username"] == "ENV_user"
    assert cfg.negative.raw["api_key"] == "ENV_key"
    # File value still wins for keys not present in env.
    assert cfg.negative.raw["sharecode"] == "FILE_share"


# ---------------------------------------------------------------------------
# Positive channel — intiface kind
# ---------------------------------------------------------------------------


def test_positive_intiface_minimal(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [positive]
        kind  = "intiface"
        label = "vibe"
        """,
    )
    cfg = load(p, env={})
    assert cfg.negative is None
    assert cfg.positive is not None
    assert cfg.positive.kind == "intiface"
    assert cfg.positive.label == "vibe"
    # Default ws_url applies when the [positive.intiface] subsection is
    # omitted entirely.
    assert cfg.positive.raw["ws_url"] == "ws://localhost:12345"
    assert cfg.positive.raw["client_name"] == "rlaif"
    assert cfg.positive.safety.spec.name == "positive"


def test_positive_intiface_full(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [positive]
        kind = "intiface"

        [positive.intiface]
        ws_url       = "ws://10.0.0.5:12345"
        client_name  = "rlaif-lab"
        device_index = 2

        [positive.safety]
        allow           = true
        max_intensity   = 80
        max_duration_s  = 10
        bucket_capacity = 8
        refill_seconds  = 30
        """,
    )
    cfg = load(p, env={})
    assert cfg.positive is not None
    assert cfg.positive.raw["ws_url"] == "ws://10.0.0.5:12345"
    assert cfg.positive.raw["client_name"] == "rlaif-lab"
    assert cfg.positive.raw["device_index"] == "2"
    assert cfg.positive.safety.allow is True
    assert cfg.positive.safety.max_intensity == 80


def test_positive_intiface_device_name_alternative(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [positive]
        kind = "intiface"

        [positive.intiface]
        device_name = "Lovense Domi"
        """,
    )
    cfg = load(p, env={})
    assert cfg.positive is not None
    assert cfg.positive.raw["device_name"] == "Lovense Domi"
    assert "device_index" not in cfg.positive.raw


def test_positive_negative_device_index_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [positive]
        kind = "intiface"

        [positive.intiface]
        device_index = -1
        """,
    )
    with pytest.raises(ConfigError, match="device_index"):
        load(p, env={})


def test_positive_intiface_ws_url_env_override(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [positive]
        kind = "intiface"

        [positive.intiface]
        ws_url = "ws://localhost:12345"
        """,
    )
    cfg = load(p, env={"RLAIF_INTIFACE_WS_URL": "ws://10.0.0.99:12345"})
    assert cfg.positive is not None
    assert cfg.positive.raw["ws_url"] == "ws://10.0.0.99:12345"


# ---------------------------------------------------------------------------
# Both channels in one config
# ---------------------------------------------------------------------------


def test_both_channels_present(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "pishock"

        [negative.pishock]
        username  = "u"
        api_key   = "k"
        sharecode = "s"

        [negative.safety]
        allow = true

        [negative.tool]
        purpose = "shock me on focus break"

        [positive]
        kind = "intiface"

        [positive.safety]
        allow = true

        [positive.tool]
        purpose = "praise me when i finish a task"
        """,
    )
    cfg = load(p, env={})
    assert cfg.negative is not None
    assert cfg.positive is not None
    assert cfg.negative.purpose == "shock me on focus break"
    assert cfg.positive.purpose == "praise me when i finish a task"
    assert cfg.negative.safety.allow is True
    assert cfg.positive.safety.allow is True


def test_neither_channel_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "# empty config\n")
    with pytest.raises(ConfigError, match="neither"):
        load(p, env={})


# ---------------------------------------------------------------------------
# Safety + tool sub-sections
# ---------------------------------------------------------------------------


def test_negative_safety_consent_raises_caps(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "pishock"

        [negative.pishock]
        username  = "u"
        api_key   = "k"
        sharecode = "s"

        [negative.safety]
        allow                    = true
        max_intensity            = 40
        i_understand_and_consent = true
        bucket_capacity          = 5
        """,
    )
    cfg = load(p, env={})
    assert cfg.negative is not None
    assert cfg.negative.safety.max_intensity == 40
    assert cfg.negative.safety.bucket_capacity == 5


def test_negative_safety_refill_floor_enforced(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "pishock"

        [negative.pishock]
        username  = "u"
        api_key   = "k"
        sharecode = "s"

        [negative.safety]
        refill_seconds = 10
        """,
    )
    with pytest.raises(ConfigError, match="refill_seconds"):
        load(p, env={})


def test_negative_safety_type_error_actionable(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "pishock"

        [negative.pishock]
        username  = "u"
        api_key   = "k"
        sharecode = "s"

        [negative.safety]
        allow = "yes"
        """,
    )
    with pytest.raises(ConfigError, match="negative.safety.*allow"):
        load(p, env={})


def test_blank_purpose_treated_as_none(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "pishock"

        [negative.pishock]
        username  = "u"
        api_key   = "k"
        sharecode = "s"

        [negative.tool]
        purpose = "   "
        """,
    )
    cfg = load(p, env={})
    assert cfg.negative is not None
    assert cfg.negative.purpose is None


# ---------------------------------------------------------------------------
# 1.x rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section",
    ["[auth]", "[provider]", "[device]", "[safety]", "[rate_limit]", "[tool]"],
)
def test_legacy_1x_section_rejected(tmp_path: Path, section: str) -> None:
    p = _write(
        tmp_path,
        f"""
        {section}
        x = 1

        [negative]
        kind = "pishock"

        [negative.pishock]
        username  = "u"
        api_key   = "k"
        sharecode = "s"
        """,
    )
    with pytest.raises(ConfigError, match="1.x schema"):
        load(p, env={})


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redacted_hides_secrets(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "pishock"

        [negative.pishock]
        username  = "alice"
        api_key   = "DEADBEEFKEY"
        sharecode = "SHARECODESHARECODE"
        """,
    )
    cfg = load(p, env={})
    red = cfg.redacted()
    raw = red["negative"]["raw"]
    assert raw["username"] == "alice"  # not a secret
    assert raw["api_key"] == "***redacted***"
    assert raw["sharecode"].startswith("SHAR") and raw["sharecode"].endswith("…")
    # Full key never appears in the redacted dump.
    assert "DEADBEEFKEY" not in str(red)


def test_redacted_hides_openshock_token(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "openshock"

        [negative.openshock]
        api_token  = "SUPER_SECRET_TOKEN"
        shocker_id = "abc-123"
        """,
    )
    cfg = load(p, env={})
    red = cfg.redacted()
    raw = red["negative"]["raw"]
    assert raw["api_token"] == "***redacted***"
    assert raw["shocker_id"] == "abc-123"  # not a secret
    assert "SUPER_SECRET_TOKEN" not in str(red)


# ---------------------------------------------------------------------------
# General error paths
# ---------------------------------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.toml", env={})


def test_invalid_toml(tmp_path: Path) -> None:
    p = tmp_path / "bad.toml"
    p.write_text("[negative\nnope", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load(p, env={})


def test_negative_label_default(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        [negative]
        kind = "pishock"

        [negative.pishock]
        username  = "u"
        api_key   = "k"
        sharecode = "s"
        """,
    )
    cfg = load(p, env={})
    assert cfg.negative is not None
    assert cfg.negative.label == "device"

"""``TB_DATA_DIR`` and the database path (spec 012, T1, AC1, AC2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_bot.config import Settings
from trading_bot.persistence.engine import DATABASE_FILENAME, database_path


def make_settings(**overrides: str) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_data_dir_defaults_to_the_data_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TB_DATA_DIR", raising=False)

    data_dir = make_settings().data_dir

    assert data_dir.is_absolute()
    assert data_dir == (Path.cwd() / "data").resolve()


def test_data_dir_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))

    assert make_settings().data_dir == tmp_path.resolve()


def test_a_relative_data_dir_is_resolved_against_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TB_DATA_DIR", "runtime/state")

    assert make_settings().data_dir == (Path.cwd() / "runtime" / "state").resolve()


def test_a_data_dir_with_a_user_prefix_is_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_DATA_DIR", "~/trading-bot-state")

    assert make_settings().data_dir == (Path.home() / "trading-bot-state").resolve()


def test_surrounding_whitespace_is_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TB_DATA_DIR", f"  {tmp_path}  ")

    assert make_settings().data_dir == tmp_path.resolve()


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_an_empty_data_dir_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TB_DATA_DIR", value)

    with pytest.raises(ValidationError, match="data_dir"):
        make_settings()


def test_a_data_dir_that_is_not_a_path_is_rejected() -> None:
    with pytest.raises(ValidationError, match="data_dir"):
        Settings(_env_file=None, data_dir=5)  # type: ignore[arg-type]


def test_data_dir_is_not_a_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """It is infrastructure detail, so it is a ``Path`` and never a redacted value."""
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))
    fake_token = "123456789" + ":" + "x" * 35

    settings = make_settings(telegram_bot_token=fake_token)

    assert str(tmp_path) not in settings.secret_values()
    assert settings.secret_values() == [fake_token]
    assert fake_token not in " ".join([repr(settings), str(settings)])


def test_the_database_file_name_is_a_constant() -> None:
    assert DATABASE_FILENAME == "trading_bot.db"


def test_the_database_path_is_the_file_inside_the_data_directory(tmp_path: Path) -> None:
    assert database_path(tmp_path) == tmp_path / "trading_bot.db"


def test_the_database_file_name_has_no_environment_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TB_DATABASE_FILENAME", "somewhere_else.db")
    monkeypatch.setenv("TB_DATABASE_PATH", "somewhere_else.db")
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path))

    settings = make_settings()

    assert database_path(settings.data_dir) == tmp_path.resolve() / DATABASE_FILENAME

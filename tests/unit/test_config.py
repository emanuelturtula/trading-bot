import logging

import pytest

from trading_bot.config import Settings
from trading_bot.logging_setup import REDACTED, RedactingFilter

# Fake secrets are assembled at runtime so no token-shaped literal lives in the repo.
FAKE_TOKEN = "987654321" + ":" + "Ab1_" * 9


def test_settings_repr_and_dump_never_expose_secrets() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        telegram_bot_token=FAKE_TOKEN,  # type: ignore[arg-type]
        session_secret="not-a-real-session-secret",  # type: ignore[arg-type]
    )

    rendered = " ".join([repr(settings), str(settings), settings.model_dump_json()])

    assert FAKE_TOKEN not in rendered
    assert "not-a-real-session-secret" not in rendered
    assert settings.secret_values() == [FAKE_TOKEN, "not-a-real-session-secret"]


def test_settings_read_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_ENVIRONMENT", "beta")
    monkeypatch.setenv("TB_VERSION", "v0.1.0-beta.1234567")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.environment == "beta"
    assert settings.version == "v0.1.0-beta.1234567"


def test_invalid_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_ENVIRONMENT", "staging")

    with pytest.raises(ValueError, match="environment"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_redacting_filter_hides_known_secrets_and_token_shapes() -> None:
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "token=%s other=%s", (FAKE_TOKEN, "s3cr3t-value"), None
    )

    RedactingFilter(["s3cr3t-value"]).filter(record)

    message = record.getMessage()
    assert FAKE_TOKEN not in message
    assert "s3cr3t-value" not in message
    assert message == f"token={REDACTED} other={REDACTED}"

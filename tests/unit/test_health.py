from fastapi.testclient import TestClient

from trading_bot.config import Settings
from trading_bot.main import create_app


def make_client(**overrides: str) -> TestClient:
    settings = Settings(_env_file=None, **overrides)  # type: ignore[arg-type]
    return TestClient(create_app(settings))


def test_health_reports_version_and_environment() -> None:
    client = make_client(version="v1.2.3-beta.abcdef1", environment="beta")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "v1.2.3-beta.abcdef1",
        "environment": "beta",
    }


def test_health_never_exposes_configuration() -> None:
    fake_token = "1234567890" + ":" + "x" * 35
    client = make_client(telegram_bot_token=fake_token)

    body = client.get("/health").text

    assert fake_token not in body
    assert "telegram" not in body.lower()


def test_api_docs_are_disabled_in_production() -> None:
    assert make_client(environment="prod").get("/docs").status_code == 404
    assert make_client(environment="dev").get("/docs").status_code == 200

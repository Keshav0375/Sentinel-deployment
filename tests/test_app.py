"""Endpoint + startup-log tests for sentinel-watchtower (deployment §2.1, §2.2).

Every test runs against settings built from a scrubbed environment with no `.env`, so a
developer's local config or an ambient APP_VERSION/DD_* never leaks into the assertions.
"""

import json
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import AppConfig

TEST_VERSION = "v9.9.9-test"
_CONFIG_ENV = ("APP_VERSION", "DD_SERVICE", "DD_ENV", "PORT")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebuild `main.settings` from a scrubbed env (APP_VERSION pinned) and no `.env`."""
    for name in _CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_VERSION", TEST_VERSION)
    monkeypatch.setattr(main, "settings", AppConfig(_env_file=None))


@pytest.fixture
def client(isolated_settings: None) -> Iterator[TestClient]:
    with TestClient(main.app) as test_client:
        yield test_client


def test_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "ok", "service": "sentinel-watchtower"}


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "uptime_seconds"}
    assert body["status"] == "ok"
    assert type(body["uptime_seconds"]) is int
    assert body["uptime_seconds"] >= 0


def test_version(client: TestClient) -> None:
    response = client.get("/version")
    assert response.status_code == 200
    assert response.json() == {
        "version": TEST_VERSION,
        "service": "sentinel-watchtower",
    }


@pytest.mark.usefixtures("isolated_settings")
def test_startup_log(capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()

    with TestClient(main.app):
        pass

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == {
        "timestamp": record["timestamp"],
        "level": "info",
        "message": "app.startup",
        "app_version": TEST_VERSION,
        "dd.service": "sentinel-watchtower",
        "dd.env": "dev",
        "dd.version": TEST_VERSION,
    }
    assert _TIMESTAMP.match(record["timestamp"])

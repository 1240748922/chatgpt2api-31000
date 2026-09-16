from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import system


def test_version_endpoint_reports_runtime_identity(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_BUILD_VERSION", "release-test-42")
    monkeypatch.setenv("CHATGPT2API_IMAGE_TAG", "sha-test42")
    monkeypatch.setenv("CHATGPT2API_BUILD_TIME", "2026-09-17T00:00:00+08:00")

    app = FastAPI()
    app.include_router(system.create_router("3.2.3"))
    response = TestClient(app).get("/version")

    assert response.status_code == 200
    assert response.json() == {
        "version": "3.2.3",
        "build_version": "release-test-42",
        "image_tag": "sha-test42",
        "build_time": "2026-09-17T00:00:00+08:00",
    }


def test_version_endpoint_has_safe_defaults(monkeypatch):
    for name in (
        "CHATGPT2API_BUILD_VERSION",
        "CHATGPT2API_IMAGE_TAG",
        "CHATGPT2API_BUILD_TIME",
    ):
        monkeypatch.delenv(name, raising=False)

    app = FastAPI()
    app.include_router(system.create_router("3.2.3"))
    payload = TestClient(app).get("/version").json()

    assert payload["version"] == "3.2.3"
    assert payload["build_version"] == "dev"
    assert payload["image_tag"] == "unknown"
    assert payload["build_time"] == "unknown"

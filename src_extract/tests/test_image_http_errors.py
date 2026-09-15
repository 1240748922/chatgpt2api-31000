from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import ai
from api.errors import install_exception_handlers
from services.image_failure import image_failure
from services.protocol.conversation import ImageGenerationError


def test_generation_http_response_preserves_upstream_rate_limit(monkeypatch):
    def upstream(payload):
        raise ImageGenerationError("upstream limited", failure=image_failure("upstream_rate_limited", retry_after=120))
    monkeypatch.setattr(ai.openai_v1_image_generations, "handle", upstream)
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(ai.create_router())
    response = TestClient(app).post("/v1/images/generations", json={"prompt": "test"},
        headers={"Authorization": "Bearer test-only"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "120"
    assert response.json()["error"]["code"] == "upstream_rate_limited"

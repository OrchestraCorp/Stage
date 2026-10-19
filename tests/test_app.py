from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": settings.app_name, "env": settings.env}


def test_health_is_liveness_only() -> None:
    response = TestClient(app).get("/health")
    assert response.headers["content-type"] == "application/json"
    assert response.json()["status"] == "ok"


def test_health_openapi_contract_is_explicit() -> None:
    operation = app.openapi()["paths"]["/health"]["get"]
    assert operation["operationId"] == "health_liveness"
    assert operation["summary"] == "Liveness probe"
    assert "database" in operation["description"]
    assert operation["responses"]["200"]["description"] == "Liveness response"
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HealthResponse"
    }

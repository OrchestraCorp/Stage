from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app


async def test_health() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": settings.app_name, "env": settings.env}


async def test_health_is_liveness_only() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
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

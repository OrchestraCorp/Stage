"""The narrow issuer contract keeps browser factors out of URLs and business bodies."""

from app.main import create_app


def test_native_oauth_paths_and_token_response_are_explicit() -> None:
    contract = create_app().openapi()
    paths = contract["paths"]
    assert paths["/oauth/authorize"]["get"]["operationId"] == "native_authorize"
    assert paths["/oauth/token"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TokenResponse"
    )
    token = contract["components"]["schemas"]["TokenResponse"]
    assert set(token["required"]) == {"access_token", "refresh_token", "id_token", "token_type", "expires_in", "scope"}
    assert token["additionalProperties"] is False
    for path in ("/oauth/login", "/oauth/token", "/oauth/revoke"):
        operation = paths[path]["post"]
        assert "application/x-www-form-urlencoded" in operation["requestBody"]["content"]
        assert not operation.get("parameters")
        failure = operation["responses"]["400"]["content"]["application/json"]["schema"]
        assert failure["required"] == ["error"] and failure["properties"]["error"]["type"] == "string"
    parameters = {entry["name"] for entry in paths["/oauth/authorize"]["get"]["parameters"]}
    assert {"code_challenge", "code_challenge_method", "state", "nonce"} <= parameters
    assert not {"password", "otp", "recovery_code", "access_token", "refresh_token"} & parameters


def test_account_routes_use_native_bearer_and_standard_stage_errors() -> None:
    paths = create_app().openapi()["paths"]
    for path in ("/api/v1/account/password", "/api/v1/account/totp/reset"):
        operation = paths[path]["post"]
        assert operation["security"] == [{"HTTPBearer": []}]
        body = operation["requestBody"]["content"]["application/json"]["schema"]
        assert body["additionalProperties"] is False
        assert not {"principal_ref", "account_ref", "organization_ref", "session_ref"} & body["properties"].keys()
        assert operation["responses"]["401"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
        if path.endswith("/password"):
            assert "204" in operation["responses"]
        else:
            assert "204" not in operation["responses"]
            assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
                "/FactorResetResponse"
            )
    response = create_app().openapi()["components"]["schemas"]["FactorResetResponse"]
    assert set(response["required"]) == {"credential_version", "enrollment_code"}
    assert response["properties"]["enrollment_code"]["type"] == "string"
    assert response["additionalProperties"] is False


def test_oidc_completion_contract_distinguishes_protocol_controls_and_account_ownership() -> None:
    contract = create_app().openapi()
    paths = contract["paths"]
    parameters = {entry["name"]: entry for entry in paths["/oauth/authorize"]["get"]["parameters"]}
    for name in ("state", "nonce", "prompt", "max_age", "response_mode", "login_hint", "acr_values"):
        assert parameters[name]["required"] is False
    assert paths["/oauth/userinfo"]["post"]["security"] == [{"HTTPBearer": []}]
    assert paths["/oauth/logout"]["get"]["operationId"] == "native_logout_confirmation"
    logout = paths["/oauth/logout"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]
    initiation, confirmation = logout["oneOf"]
    assert set(initiation["properties"]) == {"id_token_hint", "client_id", "post_logout_redirect_uri", "state"}
    assert set(confirmation["required"]) == {"request_ref", "csrf_token", "decision"}
    assert paths["/.well-known/oauth-authorization-server"]["get"]["operationId"] == "native_oauth_metadata"
    for path, method in (
        ("/api/v1/account/sessions", "get"),
        ("/api/v1/account/sessions/{session_ref}", "delete"),
        ("/api/v1/account/sessions/revoke-all", "post"),
        ("/api/v1/account/consents", "get"),
        ("/api/v1/account/consents/{client_id}", "delete"),
    ):
        operation = paths[path][method]
        assert operation["security"] == [{"HTTPBearer": []}]
        assert not {"account_ref", "principal_ref", "organization_ref"} & {
            entry["name"] for entry in operation.get("parameters", [])
        }
        assert operation["responses"]["401"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    for name in ("AccountSession", "AccountConsent"):
        assert contract["components"]["schemas"][name]["additionalProperties"] is False

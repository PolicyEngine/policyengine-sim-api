from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from policyengine_simulation_entry import auth as auth_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.schemas import CallerIdentity

from conftest import FakeBackend, make_settings


@pytest.fixture
def signing_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _token(private_key, **overrides) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": "api-v1@clients",
        "gty": "client-credentials",
        "iss": "https://issuer.example/",
        "aud": "simulation-entry",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def _use_static_signing_key(monkeypatch, public_key) -> None:
    decoder = auth_module.JWTDecoder(
        issuer="https://issuer.example/",
        audience="simulation-entry",
    )
    decoder.jwks_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=public_key)
    )
    monkeypatch.setattr(auth_module, "_decoder", lambda issuer, audience: decoder)


def test_missing_token_preserves_403_contract(monkeypatch):
    class Decoder:
        def __call__(self, token, runtime):
            from fastapi import HTTPException

            raise HTTPException(status_code=403)

    monkeypatch.setattr(auth_module, "_decoder", lambda issuer, audience: Decoder())
    app = create_app(settings=make_settings(), backend=FakeBackend())

    with TestClient(app) as client:
        result = client.get("/jobs/job-1")

    assert result.status_code == 403


def test_public_routes_do_not_require_token(monkeypatch):
    class Decoder:
        def __call__(self, token, runtime):
            raise AssertionError("public route attempted authentication")

    monkeypatch.setattr(auth_module, "_decoder", lambda issuer, audience: Decoder())
    app = create_app(settings=make_settings(), backend=FakeBackend())

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/versions").status_code == 200
        assert client.post("/ping", json={"value": 1}).status_code == 200


def test_valid_api_v1_token_can_poll(monkeypatch, signing_keys):
    private_key, public_key = signing_keys
    _use_static_signing_key(monkeypatch, public_key)
    app = create_app(settings=make_settings(), backend=FakeBackend())

    with TestClient(app) as client:
        result = client.get(
            "/jobs/job-1",
            headers={"Authorization": f"Bearer {_token(private_key)}"},
        )

    assert result.status_code == 200


def test_decoder_returns_only_the_declared_caller_identity(signing_keys):
    private_key, public_key = signing_keys
    decoder = auth_module.JWTDecoder(
        issuer="https://issuer.example/",
        audience="simulation-entry",
    )
    decoder.jwks_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=public_key)
    )

    identity = decoder(
        HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials=_token(
                private_key,
                scope="simulate:read",
                undocumented_claim="ignored",
            ),
        ),
        Mock(),
    )

    assert isinstance(identity, CallerIdentity)
    assert identity.subject == "api-v1@clients"
    assert identity.grant_type == "client-credentials"
    assert identity.scope == "simulate:read"
    assert "undocumented_claim" not in identity.model_dump()


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"iss": "https://wrong-issuer.example/"},
        {"aud": "wrong-audience"},
        {"exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
    ],
    ids=["wrong-issuer", "wrong-audience", "expired"],
)
def test_invalid_api_v1_token_claims_return_403(
    monkeypatch,
    signing_keys,
    claim_overrides,
):
    private_key, public_key = signing_keys
    _use_static_signing_key(monkeypatch, public_key)
    app = create_app(settings=make_settings(), backend=FakeBackend())

    with TestClient(app) as client:
        result = client.get(
            "/jobs/job-1",
            headers={
                "Authorization": f"Bearer {_token(private_key, **claim_overrides)}"
            },
        )

    assert result.status_code == 403


def test_malformed_token_returns_403(monkeypatch, signing_keys):
    _, public_key = signing_keys
    _use_static_signing_key(monkeypatch, public_key)
    app = create_app(settings=make_settings(), backend=FakeBackend())

    with TestClient(app) as client:
        result = client.get(
            "/jobs/job-1",
            headers={"Authorization": "Bearer not-a-jwt"},
        )

    assert result.status_code == 403


ALL_PERMISSIONS = ("simulation:submit", "simulation:read", "stage12:operate")
COMPARISON_PAYLOAD = {"country": "us", "scope": "macro", "reform": {}}
BUDGET_WINDOW_PAYLOAD = {
    "country": "us",
    "region": "us",
    "scope": "macro",
    "reform": {},
    "start_year": "2026",
    "window_size": 1,
}
STAGE12_REPORT_PATH = "/internal/stage12/reports/00000000-0000-0000-0000-000000000001"

# Each protected route, the permission it requires, and the status its handler
# returns once authorized. Stage 12 is disabled and unconfigured in these
# tests, so an authorized Stage 12 request reaches the handler's 503 response.
PROTECTED_ROUTES = [
    pytest.param(
        "post",
        "/simulate/economy/comparison",
        COMPARISON_PAYLOAD,
        "simulation:submit",
        200,
        id="submit-comparison",
    ),
    pytest.param(
        "post",
        "/simulate/economy/budget-window",
        BUDGET_WINDOW_PAYLOAD,
        "simulation:submit",
        200,
        id="submit-budget-window",
    ),
    pytest.param(
        "get",
        "/jobs/job-1",
        None,
        "simulation:read",
        200,
        id="poll-job",
    ),
    pytest.param(
        "get",
        "/budget-window-jobs/batch-1",
        None,
        "simulation:read",
        200,
        id="poll-budget-window",
    ),
    pytest.param(
        "post",
        "/internal/stage12/reports",
        COMPARISON_PAYLOAD,
        "stage12:operate",
        503,
        id="stage12-submit",
    ),
    pytest.param(
        "get",
        STAGE12_REPORT_PATH,
        None,
        "stage12:operate",
        503,
        id="stage12-status",
    ),
]


def _identity(
    *,
    permissions: list[str] | None = None,
    scope: str | None = None,
    azp: str | None = "api-v1-client",
    sub: str = "api-v1-client@clients",
    gty: str | None = None,
) -> CallerIdentity:
    return CallerIdentity.model_validate(
        {
            "sub": sub,
            "iss": "https://issuer.example/",
            "aud": "simulation-entry",
            "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
            "permissions": permissions,
            "scope": scope,
            "azp": azp,
            "gty": gty,
        }
    )


def _app_for(
    identity: CallerIdentity | None,
    *,
    enforce: bool,
    backend: FakeBackend | None = None,
):
    return create_app(
        settings=make_settings(enforce_permissions=enforce),
        backend=backend or FakeBackend(),
        auth_dependency=lambda: identity,
    )


def _send(client: TestClient, method: str, path: str, body):
    kwargs = {"json": body} if body is not None else {}
    return getattr(client, method)(path, **kwargs)


def _record_events(client: TestClient, monkeypatch) -> Mock:
    runtime = client.app.state.policyengine_observability
    event = Mock(wraps=runtime.event)
    monkeypatch.setattr(runtime, "event", event)
    return event


def _permission_events(event: Mock) -> list[dict[str, object]]:
    return [
        call.kwargs["attributes"]
        for call in event.call_args_list
        if call.args == ("simulation_entry_permission_missing",)
    ]


@pytest.mark.parametrize(
    ("method", "path", "body", "permission", "authorized_status"),
    PROTECTED_ROUTES,
)
def test_enforced_route_rejects_caller_without_its_permission(
    method,
    path,
    body,
    permission,
    authorized_status,
):
    backend = FakeBackend()
    other_permissions = [item for item in ALL_PERMISSIONS if item != permission]
    app = _app_for(
        _identity(
            permissions=other_permissions,
            scope=" ".join(other_permissions),
        ),
        enforce=True,
        backend=backend,
    )

    with TestClient(app) as client:
        result = _send(client, method, path, body)

    assert result.status_code == 403
    assert backend.requests == []


@pytest.mark.parametrize(
    ("method", "path", "body", "permission", "authorized_status"),
    PROTECTED_ROUTES,
)
def test_enforced_route_accepts_caller_with_only_its_permission(
    method,
    path,
    body,
    permission,
    authorized_status,
):
    app = _app_for(_identity(permissions=[permission]), enforce=True)

    with TestClient(app) as client:
        result = _send(client, method, path, body)

    assert result.status_code == authorized_status


@pytest.mark.parametrize(
    "claims",
    [
        {"permissions": ["simulation:read"]},
        {"scope": "simulation:read"},
        {"scope": "openid simulation:submit simulation:read"},
        {"permissions": ["simulation:submit"], "scope": "simulation:read"},
    ],
    ids=[
        "permissions-claim",
        "scope-claim",
        "space-delimited-scope",
        "either-claim",
    ],
)
def test_permission_may_be_granted_by_permissions_or_scope_claim(claims):
    app = _app_for(_identity(**claims), enforce=True)

    with TestClient(app) as client:
        result = client.get("/jobs/job-1")

    assert result.status_code == 200


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"permissions": [], "scope": ""},
        {"permissions": ["simulation:read-all"]},
        {"scope": "simulation:reader simulation"},
        {"scope": "simulation:submit,simulation:read"},
    ],
    ids=[
        "no-claims",
        "empty-claims",
        "permission-prefix",
        "scope-prefix",
        "comma-delimited-scope",
    ],
)
def test_permission_requires_an_exact_claim_entry(claims):
    app = _app_for(_identity(**claims), enforce=True)

    with TestClient(app) as client:
        result = client.get("/jobs/job-1")

    assert result.status_code == 403


@pytest.mark.parametrize(
    ("claims", "expected_status"),
    [
        ({"sub": "google-oauth2|123", "scope": "openid simulation:read"}, 403),
        ({"sub": "google-oauth2|123", "permissions": ["simulation:read"]}, 200),
        (
            {
                "sub": "api-v1-client",
                "gty": "client-credentials",
                "scope": "simulation:read",
            },
            200,
        ),
    ],
    ids=[
        "user-token-scope-ignored",
        "user-token-permissions-claim",
        "client-credentials-grant-scope",
    ],
)
def test_scope_claim_counts_only_for_client_credentials_tokens(
    claims,
    expected_status,
):
    app = _app_for(_identity(**claims), enforce=True)

    with TestClient(app) as client:
        result = client.get("/jobs/job-1")

    assert result.status_code == expected_status


def test_enforced_missing_permission_records_permission_and_client(monkeypatch):
    app = _app_for(
        _identity(permissions=["simulation:read"], azp="api-v1-client"),
        enforce=True,
    )

    with TestClient(app) as client:
        event = _record_events(client, monkeypatch)
        result = client.post("/simulate/economy/comparison", json=COMPARISON_PAYLOAD)

    assert result.status_code == 403
    assert _permission_events(event) == [
        {
            "permission": "simulation:submit",
            "enforced": True,
            "client_id": "api-v1-client",
        }
    ]


@pytest.mark.parametrize(
    ("method", "path", "body", "permission", "authorized_status"),
    PROTECTED_ROUTES,
)
def test_log_only_mode_allows_and_records_missing_permission(
    monkeypatch,
    method,
    path,
    body,
    permission,
    authorized_status,
):
    app = _app_for(_identity(permissions=[], azp="unmigrated-client"), enforce=False)

    with TestClient(app) as client:
        event = _record_events(client, monkeypatch)
        result = _send(client, method, path, body)

    assert result.status_code == authorized_status
    assert _permission_events(event) == [
        {
            "permission": permission,
            "enforced": False,
            "client_id": "unmigrated-client",
        }
    ]


def test_permission_event_omits_absent_client_identifier(monkeypatch):
    app = _app_for(_identity(azp=None), enforce=False)

    with TestClient(app) as client:
        event = _record_events(client, monkeypatch)
        result = client.get("/jobs/job-1")

    assert result.status_code == 200
    assert _permission_events(event) == [
        {"permission": "simulation:read", "enforced": False}
    ]


def test_granted_permission_records_no_permission_event(monkeypatch):
    app = _app_for(_identity(permissions=["simulation:read"]), enforce=True)

    with TestClient(app) as client:
        event = _record_events(client, monkeypatch)
        result = client.get("/jobs/job-1")

    assert result.status_code == 200
    assert _permission_events(event) == []


@pytest.mark.parametrize(
    ("enforce", "expected_status"),
    [(False, 200), (True, 403)],
    ids=["log-only", "enforced"],
)
def test_permission_telemetry_failure_does_not_change_response(
    monkeypatch,
    enforce,
    expected_status,
):
    app = _app_for(_identity(permissions=[]), enforce=enforce)

    with TestClient(app) as client:
        runtime = client.app.state.policyengine_observability
        emit = runtime.event

        def fail_permission_event(name, **kwargs):
            if name == "simulation_entry_permission_missing":
                raise RuntimeError("telemetry unavailable")
            return emit(name, **kwargs)

        failing_event = Mock(side_effect=fail_permission_event)
        monkeypatch.setattr(runtime, "event", failing_event)
        result = client.get("/jobs/job-1")

    assert result.status_code == expected_status
    assert _permission_events(failing_event) == [
        {
            "permission": "simulation:read",
            "enforced": enforce,
            "client_id": "api-v1-client",
        }
    ]


@pytest.mark.parametrize(
    ("method", "path", "body", "permission", "authorized_status"),
    PROTECTED_ROUTES,
)
def test_disabled_authentication_allows_enforced_routes(
    monkeypatch,
    method,
    path,
    body,
    permission,
    authorized_status,
):
    class Decoder:
        def __call__(self, token, runtime):
            raise AssertionError("disabled authentication decoded a token")

    monkeypatch.setattr(auth_module, "_decoder", lambda issuer, audience: Decoder())
    app = create_app(
        settings=make_settings(
            auth_required=False,
            auth_issuer="",
            auth_audience="",
            enforce_permissions=True,
        ),
        backend=FakeBackend(),
    )

    with TestClient(app) as client:
        event = _record_events(client, monkeypatch)
        result = _send(client, method, path, body)

    assert result.status_code == authorized_status
    assert _permission_events(event) == []


def test_public_routes_stay_public_when_permissions_are_enforced(monkeypatch):
    class Decoder:
        def __call__(self, token, runtime):
            raise AssertionError("public route attempted authentication")

    monkeypatch.setattr(auth_module, "_decoder", lambda issuer, audience: Decoder())
    app = create_app(
        settings=make_settings(enforce_permissions=True),
        backend=FakeBackend(),
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
        assert client.get("/versions").status_code == 200
        assert client.get("/versions/us").status_code == 200
        assert client.post("/ping", json={"value": 1}).status_code == 200


def test_enforced_stage12_routes_reject_simulation_permissions():
    app = _app_for(
        _identity(
            permissions=["simulation:submit", "simulation:read"],
            scope="simulation:submit simulation:read",
        ),
        enforce=True,
    )

    with TestClient(app) as client:
        submission = client.post(
            "/internal/stage12/reports",
            json=COMPARISON_PAYLOAD,
        )
        status = client.get(STAGE12_REPORT_PATH)

    assert submission.status_code == 403
    assert status.status_code == 403


@pytest.mark.parametrize(
    ("claims", "expected_status"),
    [
        ({}, 403),
        ({"permissions": ["simulation:submit"]}, 403),
        ({"permissions": ["simulation:read"]}, 200),
        ({"scope": "simulation:read"}, 200),
    ],
    ids=["no-permission", "wrong-permission", "permissions-claim", "scope-claim"],
)
def test_enforced_permissions_apply_to_verified_api_v1_tokens(
    monkeypatch,
    signing_keys,
    claims,
    expected_status,
):
    private_key, public_key = signing_keys
    _use_static_signing_key(monkeypatch, public_key)
    app = create_app(
        settings=make_settings(enforce_permissions=True),
        backend=FakeBackend(),
    )

    with TestClient(app) as client:
        result = client.get(
            "/jobs/job-1",
            headers={"Authorization": f"Bearer {_token(private_key, **claims)}"},
        )

    assert result.status_code == expected_status


def test_permissions_are_logged_not_enforced_by_default(monkeypatch, signing_keys):
    private_key, public_key = signing_keys
    _use_static_signing_key(monkeypatch, public_key)
    app = create_app(settings=make_settings(), backend=FakeBackend())

    with TestClient(app) as client:
        event = _record_events(client, monkeypatch)
        result = client.get(
            "/jobs/job-1",
            headers={
                "Authorization": f"Bearer {_token(private_key, azp='api-v1-client')}"
            },
        )

    assert result.status_code == 200
    assert _permission_events(event) == [
        {
            "permission": "simulation:read",
            "enforced": False,
            "client_id": "api-v1-client",
        }
    ]

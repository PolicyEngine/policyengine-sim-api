"""Gateway observability identity and request-correlation tests."""

import json

from policyengine_observability import REQUEST_ID_HEADER
from policyengine_observability.runtime import REQUEST_LOGGER
from policyengine_simulation_gateway.testing import create_gateway_app


def test_gateway_reads_policyengine_request_id_into_observability_context(
    monkeypatch,
):
    records = []
    monkeypatch.setattr(
        REQUEST_LOGGER,
        "info",
        lambda message: records.append(json.loads(message)),
    )
    app = create_gateway_app()

    from fastapi.testclient import TestClient

    response = TestClient(app).get(
        "/health",
        headers={REQUEST_ID_HEADER: "request-through-entrypoint"},
    )

    assert response.status_code == 200
    assert len(records) == 1
    assert records[0]["request_id"] == "request-through-entrypoint"
    assert records[0]["service_name"] == "policyengine-simulation-gateway"
    assert records[0]["service_role"] == "modal_gateway"


def test_gateway_uses_its_own_service_name():
    app = create_gateway_app()

    assert (
        app.state.policyengine_observability.config.service_name
        == "policyengine-simulation-gateway"
    )

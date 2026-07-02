"""Tests for the household calculation endpoint.

The calculate test loads the policyengine-us tax-benefit system, so the
first run takes a while.
"""

import pytest
from fastapi.testclient import TestClient

from policyengine_api_household.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_ping_started(client):
    response = client.get("/ping/started")
    assert response.status_code == 200
    assert response.json() == "alive"


def test_ping_alive(client):
    response = client.get("/ping/alive")
    assert response.status_code == 200
    assert response.json()["healthy"] is True


def test_calculate_us_single_adult_income_tax(client):
    """A single US adult earning 30k in 2026: null income_tax is computed."""
    payload = {
        "household": {
            "people": {
                "you": {
                    "age": {"2026": 30},
                    "employment_income": {"2026": 30_000},
                }
            },
            "tax_units": {
                "your tax unit": {
                    "members": ["you"],
                    # null means "compute this" (income_tax is a
                    # tax-unit-level variable in policyengine-us).
                    "income_tax": {"2026": None},
                }
            },
            "spm_units": {"your spm unit": {"members": ["you"]}},
            "families": {"your family": {"members": ["you"]}},
            "marital_units": {"your marital unit": {"members": ["you"]}},
            "households": {
                "your household": {
                    "members": ["you"],
                    "state_name": {"2026": "TX"},
                }
            },
        }
    }

    response = client.post("/us/calculate", json=payload)
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["status"] == "ok"
    assert body["message"] is None

    # Model-version block for parity diffs.
    bundle = body["policyengine_bundle"]
    assert isinstance(bundle["model_version"], str)
    assert bundle["model_version"] != ""

    # The requested null was filled with a numeric result.
    income_tax = body["result"]["tax_units"]["your tax unit"]["income_tax"]["2026"]
    assert isinstance(income_tax, (int, float))
    assert income_tax > 0

    # Inputs echo back unchanged.
    assert body["result"]["people"]["you"]["employment_income"]["2026"] == (30_000)
    assert body["result"]["households"]["your household"]["state_name"]["2026"] == "TX"


def test_calculate_rejects_invalid_household(client):
    response = client.post(
        "/us/calculate",
        json={"household": {"people": {"you": {}}}},
    )
    # Missing required "households" entity group fails schema validation.
    assert response.status_code == 400
    assert response.json()["status"] == "error"

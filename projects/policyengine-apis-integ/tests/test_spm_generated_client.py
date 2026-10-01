"""Hermetic wire checks against the actual generated public Python client."""

import json

import pytest

from policyengine_api_simulation_client.models import (
    SPMCapability,
    SPMComparisonProvenance,
    SPMSelection,
    SimulationRequest,
)


def test_omitted_selection_options_stay_omitted_in_generated_requests():
    selection = SPMSelection(scenario="zero_real")
    assert selection.to_dict() == {"scenario": "zero_real"}
    request = SimulationRequest.from_dict({"country": "us", "spm": selection.to_dict()})
    assert json.loads(json.dumps(request.to_dict()))["spm"] == {"scenario": "zero_real"}


def test_explicit_null_date_survives_generated_request_roundtrip():
    selected = {"geography_kind": "national", "as_of": None}
    request = SimulationRequest.from_dict({"country": "us", "spm": selected})
    assert request.to_dict()["spm"] == selected


def test_generated_capability_keeps_and_enforces_contract_version():
    capability = {
        "contract_version": "canonical-spm-v1",
        "defaults": {
            "forecast_content_sha256": "a" * 64,
            "scenario": "ce_trend",
            "geography_kind": "national",
            "geography_id": None,
            "county_vintage": "2020",
            "as_of": None,
        },
    }
    assert SPMCapability.from_dict(capability).to_dict() == capability
    with pytest.raises(ValueError, match="contract_version"):
        SPMCapability.from_dict({**capability, "contract_version": "unknown-v2"})


def test_generated_receipts_use_only_compact_typed_metadata():
    receipt = {
        "schema_version": "canonical-spm-provenance-v2",
        "forecast_id": "test-only",
        "forecast_sha256": "a" * 64,
        "scenario": "ce_trend",
        "geography_kind": "national",
        "geography_id": None,
        "county_vintage": "2020",
        "as_of": None,
        "years": ["2026"],
        "runtime_versions": {
            "policyengine": "6.2.1",
            "policyengine-core": "3.32.10",
            "policyengine-us": "2.2.1",
            "spm-calculator": "1.0.0",
        },
    }
    provenance = {
        "schema_version": "canonical-spm-comparison-v2",
        "baseline": {"receipt": receipt, "execution_count": 20},
        "reform": {"receipt": receipt, "execution_count": 20},
    }
    assert SPMComparisonProvenance.from_dict(provenance).to_dict() == provenance


def test_generated_comparison_rejects_old_receipt_lists():
    with pytest.raises((KeyError, TypeError, ValueError)):
        SPMComparisonProvenance.from_dict({"baseline": [], "reform": []})

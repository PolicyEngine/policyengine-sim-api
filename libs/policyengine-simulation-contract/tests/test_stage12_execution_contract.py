"""Consumer compatibility checks for the canonical Stage 12 contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactDescriptor,
    AggregateReportArtifactPayload,
    CONTRACT_ID,
    CONTRACT_VERSION,
    EvaluationReportRecord,
    EvaluationSimulationRecord,
    ReportExecutionInput,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
    SimulationParquetPayloadContract,
)

CANONICAL_CONTRACT_SHA256 = (
    "d7e41ca1aade56acd9f8177be77a93eb862296a238d08b9f39e0115a9cc49ffd"
)
SCHEMA_MODELS = (
    AggregateReportArtifactDescriptor,
    AggregateReportArtifactPayload,
    EvaluationReportRecord,
    EvaluationSimulationRecord,
    ReportExecutionInput,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
    SimulationParquetPayloadContract,
)


def _canonical_contract_path() -> Path:
    return (
        Path(__file__).resolve().parents[4]
        / "policyengine-api/docs/generated/stage12_worker_contracts.json"
    )


def test_consumer_declares_the_canonical_contract_identity() -> None:
    assert CONTRACT_ID == ("https://policyengine.org/contracts/stage-12-worker-v1.json")
    assert CONTRACT_VERSION == 1


def test_consumer_models_match_canonical_schemas_when_repositories_are_adjacent() -> (
    None
):
    path = _canonical_contract_path()
    if not path.exists():
        pytest.skip("canonical policyengine-api contract is not checked out adjacent")
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CANONICAL_CONTRACT_SHA256
    canonical = json.loads(raw)["schemas"]
    assert {model.__name__: model.model_json_schema() for model in SCHEMA_MODELS} == (
        canonical
    )

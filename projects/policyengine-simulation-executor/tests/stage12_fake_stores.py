"""In-memory comparison and artifact stores for the Stage 12 tests."""

from __future__ import annotations

from hashlib import sha256
from threading import Lock

import pandas as pd
from fixtures.fake_object_store import InMemoryObjectStore
from policyengine_simulation_contract.stage12_execution import (
    ArtifactMediaType,
    ArtifactReference,
    ComparisonReportPersistenceResult,
    ComparisonSimulationPersistenceResult,
    SimulationArtifactDescriptor,
    stage12_output_plan_sha256,
)
from policyengine_simulation_executor.stage12_artifacts import (
    Stage12ArtifactStore,
    canonical_json_bytes,
    serialize_simulation_frames,
)
from stage12_runtime_support import EVALUATION_ID, shared_output_plan


class FakeStore:
    def __init__(self):
        self.parent = None
        self.children = {}
        self.lock = Lock()

    def create_or_resolve_report(self, record):
        with self.lock:
            if self.parent is None:
                self.parent = record
                return ComparisonReportPersistenceResult(
                    record=record,
                    created=True,
                )
            return ComparisonReportPersistenceResult(
                record=self.parent,
                created=False,
            )

    def get_report(self, evaluation_id):
        assert evaluation_id == EVALUATION_ID
        assert self.parent is not None
        return self.parent

    def replace_report(self, record):
        self.parent = record
        return record

    def replace_report_result_comparison(self, record):
        self.parent = record
        return record

    def create_or_resolve_simulation(self, record):
        with self.lock:
            existing = self.children.get(record.simulation_execution_id)
            if existing is None:
                self.children[record.simulation_execution_id] = record
                return ComparisonSimulationPersistenceResult(
                    record=record, created=True
                )
            return ComparisonSimulationPersistenceResult(
                record=existing,
                created=False,
            )

    def get_simulation(self, simulation_execution_id):
        return self.children[simulation_execution_id]

    def replace_simulation(self, record):
        with self.lock:
            self.children[record.simulation_execution_id] = record
        return record

    def replace_simulation_if_status(self, record, *, expected_status):
        with self.lock:
            current = self.children[record.simulation_execution_id]
            if current.status is not expected_status:
                return current, False
            self.children[record.simulation_execution_id] = record
        return record, True

    def attach_simulation_invocation(
        self,
        simulation_execution_id,
        *,
        expected_placeholder,
        modal_invocation_id,
        updated_at,
    ):
        with self.lock:
            child = self.children[simulation_execution_id]
            assert child.modal_invocation_id == expected_placeholder
            child = child.model_copy(
                update={
                    "modal_invocation_id": modal_invocation_id,
                    "updated_at": updated_at,
                }
            )
            self.children[simulation_execution_id] = child
        return child


class FakeArtifacts:
    def __init__(self):
        self.payloads = {}
        self.aggregate_writes = []
        self.comparison_writes = []
        self.input_writes = []
        # Aggregates go through the real store, so every coordinator test
        # enforces the aggregate artifact contract that production enforces.
        self.report_store = Stage12ArtifactStore("private", store=InMemoryObjectStore())

    def write_input(self, *, prefix, simulation):
        self.input_writes.append((prefix, simulation.role))

    def add_simulation(self, simulation, frames=None):
        payload, row_identity = serialize_simulation_frames(
            frames or simulation_frames()
        )
        digest = sha256(payload).hexdigest()
        uri = f"gs://private/{simulation.role.value}.parquet"
        self.payloads[uri] = payload
        return SimulationArtifactDescriptor(
            evaluation_id=simulation.evaluation_id,
            simulation_execution_id=simulation.simulation_execution_id,
            role=simulation.role,
            artifact=ArtifactReference(
                uri=uri,
                media_type=ArtifactMediaType.PARQUET,
                content_sha256=digest,
                size_bytes=len(payload),
            ),
            output_plan_sha256=stage12_output_plan_sha256(shared_output_plan()),
            row_identity=row_identity,
            bundle=simulation.bundle,
        )

    def write_simulation(
        self,
        *,
        prefix,
        simulation,
        frames,
        calculation_provenance=None,
        uk_local_authority_metadata=None,
    ):
        payload, row_identity = serialize_simulation_frames(
            frames,
            calculation_provenance=calculation_provenance,
            uk_local_authority_metadata=uk_local_authority_metadata,
        )
        uri = f"gs://private/{simulation.role.value}.parquet"
        self.payloads[uri] = payload
        descriptor = SimulationArtifactDescriptor(
            evaluation_id=simulation.evaluation_id,
            simulation_execution_id=simulation.simulation_execution_id,
            role=simulation.role,
            artifact=ArtifactReference(
                uri=uri,
                media_type=ArtifactMediaType.PARQUET,
                content_sha256=sha256(payload).hexdigest(),
                size_bytes=len(payload),
            ),
            output_plan_sha256=stage12_output_plan_sha256(shared_output_plan()),
            row_identity=row_identity,
            bundle=simulation.bundle,
        )
        return descriptor.model_copy(
            update={"calculation_provenance": calculation_provenance}
        )

    def read(self, uri):
        if uri in self.payloads:
            return self.payloads[uri]
        return self.report_store.read(uri)

    def write_aggregate(self, *, prefix, payload):
        reference = self.report_store.write_aggregate(prefix=prefix, payload=payload)
        self.aggregate_writes.append(payload)
        return reference

    def write_comparison(self, *, prefix, payload):
        encoded = canonical_json_bytes(payload)
        digest = sha256(encoded).hexdigest()
        uri = "gs://private/comparison.json"
        self.payloads[uri] = encoded
        self.comparison_writes.append(payload)
        return ArtifactReference(
            uri=uri,
            media_type=ArtifactMediaType.JSON,
            content_sha256=digest,
            size_bytes=len(encoded),
        )


def simulation_frames(value=100.0):
    return {
        "household": pd.DataFrame(
            {"household_id": [1], "household_net_income": [value]}
        ),
        "person": pd.DataFrame({"person_id": [1], "household_id": [1], "age": [40]}),
    }

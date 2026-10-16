"""Job status stage fields.

The worker writes a stage record; the gateway adds optional stage fields to
``GET /jobs/{job_id}``. Jobs routed to workers that do not declare the
capability keep their existing response body.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from policyengine_simulation_contract.job_progress import (
    JOB_PROGRESS_CAPABILITY_VERSION,
    JobExecution,
    JobProgressRecord,
    JobSegmentProgress,
    JobStage,
)
from policyengine_simulation_contract.job_progress_state import (
    JOB_PROGRESS_DICT_NAME,
)
from policyengine_simulation_observability.stages import Stage

JOB_METADATA_DICT_NAME = "simulation-api-job-metadata"
LEGACY_RUNNING_KEYS = {
    "status",
    "result",
    "error",
    "resolved_app_name",
    "policyengine_bundle",
}


@pytest.fixture
def stage_reporting_worker(mock_modal):
    """Declare the capability on the routing state's latest bundle."""
    bundles = mock_modal["dicts"]["simulation-api-routing-state"]["active"]["bundles"]
    bundles["4.10.0"] = {
        **bundles["4.10.0"],
        "job_progress": JOB_PROGRESS_CAPABILITY_VERSION,
    }
    return mock_modal


def _submit(client: TestClient) -> str:
    response = client.post(
        "/simulate/economy/comparison",
        json={"country": "us", "scope": "macro", "reform": {}},
    )
    assert response.status_code == 200
    return response.json()["job_id"]


def _store_record(mock_modal, job_id: str, record: JobProgressRecord) -> None:
    mock_modal["dicts"].setdefault(JOB_PROGRESS_DICT_NAME, {})[job_id] = (
        record.model_dump(mode="json")
    )


def _reform_record(**overrides) -> JobProgressRecord:
    values = {
        "stage": JobStage.REFORM,
        "execution": JobExecution.MONOLITHIC,
        "started_at": "2026-10-13T12:00:30Z",
        "stage_started_at": "2026-10-13T12:01:00Z",
        "stage_durations": {JobStage.LOADING_DATA: 12.5, JobStage.BASELINE: 17.5},
    }
    return JobProgressRecord.model_validate({**values, **overrides})


class TestSubmission:
    def test__records_submission_time_and_worker_capability(
        self, stage_reporting_worker, client: TestClient
    ):
        job_id = _submit(client)

        metadata = stage_reporting_worker["dicts"][JOB_METADATA_DICT_NAME][job_id]
        assert datetime.fromisoformat(metadata["submitted_at"]).tzinfo is not None
        assert metadata["job_progress"] == JOB_PROGRESS_CAPABILITY_VERSION

    def test__worker_without_capability_is_recorded_as_none(
        self, mock_modal, client: TestClient
    ):
        job_id = _submit(client)

        metadata = mock_modal["dicts"][JOB_METADATA_DICT_NAME][job_id]
        assert metadata["job_progress"] is None


class TestStageReportingWorker:
    def test__running_job_without_record_is_queued(
        self, stage_reporting_worker, client: TestClient
    ):
        job_id = _submit(client)
        stage_reporting_worker["func"].last_call.running = True

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "running"
        assert body["stage"] == "queued"
        assert body["progress"] == 0
        assert body["progress_max"] == 24
        assert body["submitted_at"].endswith("Z")
        assert "job_progress" not in body

    def test__running_job_reports_the_worker_stage(
        self, stage_reporting_worker, client: TestClient
    ):
        job_id = _submit(client)
        stage_reporting_worker["func"].last_call.running = True
        _store_record(stage_reporting_worker, job_id, _reform_record())

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 202
        body = response.json()
        assert body["stage"] == "reform"
        assert body["progress"] == 67
        assert body["progress_max"] == 96
        assert body["started_at"] == "2026-10-13T12:00:30Z"
        assert body["stage_started_at"] == "2026-10-13T12:01:00Z"
        assert body["stage_durations"] == {"loading_data": 12.5, "baseline": 17.5}
        assert body["result"] is None
        assert body["error"] is None

    def test__segmented_run_reports_segments_without_moving_progress(
        self, stage_reporting_worker, client: TestClient
    ):
        job_id = _submit(client)
        stage_reporting_worker["func"].last_call.running = True
        _store_record(
            stage_reporting_worker,
            job_id,
            _reform_record(
                stage=JobStage.SIMULATING,
                execution=JobExecution.SEGMENTED,
                segments=JobSegmentProgress(completed=10, total=20),
            ),
        )

        body = client.get(f"/jobs/{job_id}").json()

        assert body["stage"] == "simulating"
        assert body["segments"] == {"completed": 10, "total": 20}
        assert body["progress"] == 5
        assert body["progress_max"] == 96

    def test__complete_job_reports_durations_and_no_current_stage(
        self, stage_reporting_worker, client: TestClient
    ):
        job_id = _submit(client)
        _store_record(
            stage_reporting_worker,
            job_id,
            _reform_record(
                stage=JobStage.OUTPUTS,
                finished_at="2026-10-13T12:04:00Z",
                stage_durations={
                    JobStage.LOADING_DATA: 12.5,
                    JobStage.BASELINE: 17.5,
                    JobStage.REFORM: 150.0,
                    JobStage.OUTPUTS: 30.0,
                },
            ),
        )

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "complete"
        assert body["result"] is not None
        assert body["progress"] == 100
        assert "progress_max" not in body
        assert body["finished_at"] == "2026-10-13T12:04:00Z"
        assert body["stage_durations"]["reform"] == 150.0
        assert "stage" not in body
        assert "stage_started_at" not in body

    def test__failed_job_reports_the_stage_it_stopped_in(
        self, stage_reporting_worker, client: TestClient
    ):
        job_id = _submit(client)
        stage_reporting_worker["func"].last_call.error = RuntimeError("worker crashed")
        _store_record(
            stage_reporting_worker,
            job_id,
            _reform_record(finished_at="2026-10-13T12:02:00Z"),
        )

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 500
        body = response.json()
        assert body["status"] == "failed"
        assert body["stage"] == "reform"
        assert "progress" not in body
        assert "worker crashed" not in body["error"]

    def test__progress_read_uses_the_registered_stage(
        self, stage_reporting_worker, client: TestClient, monkeypatch
    ):
        job_id = _submit(client)
        stage_reporting_worker["func"].last_call.running = True
        runtime = client.app.state.policyengine_observability
        original_span = runtime.span
        names = []

        def recording_span(name, *args, **kwargs):
            names.append(name)
            return original_span(name, *args, **kwargs)

        monkeypatch.setattr(runtime, "span", recording_span)

        client.get(f"/jobs/{job_id}")

        assert Stage.MODAL_JOB_PROGRESS_READ.value in names


class TestStageReportingFailuresAreHarmless:
    def test__unreadable_store_drops_stage_fields(
        self, stage_reporting_worker, client: TestClient, monkeypatch
    ):
        from policyengine_simulation_gateway import endpoints

        job_id = _submit(client)
        stage_reporting_worker["func"].last_call.running = True

        def failing_read(job_id):
            msg = "modal unavailable"
            raise ConnectionError(msg)

        monkeypatch.setattr(endpoints, "get_job_progress", failing_read)

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "running"
        assert "stage" not in body
        assert "progress" not in body
        assert "submitted_at" in body

    def test__malformed_record_drops_stage_fields(
        self, stage_reporting_worker, client: TestClient
    ):
        job_id = _submit(client)
        stage_reporting_worker["func"].last_call.running = True
        stage_reporting_worker["dicts"].setdefault(JOB_PROGRESS_DICT_NAME, {})[
            job_id
        ] = {"stage": "teleporting"}

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 202
        assert "stage" not in response.json()

    def test__malformed_record_keeps_a_complete_result(
        self, stage_reporting_worker, client: TestClient
    ):
        job_id = _submit(client)
        stage_reporting_worker["dicts"].setdefault(JOB_PROGRESS_DICT_NAME, {})[
            job_id
        ] = {"schema_version": 99}

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 200
        assert response.json()["status"] == "complete"


class TestWorkerWithoutCapability:
    def test__running_body_adds_only_the_submission_time(
        self, mock_modal, client: TestClient
    ):
        job_id = _submit(client)
        mock_modal["func"].last_call.running = True
        _store_record(mock_modal, job_id, _reform_record())

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 202
        assert set(response.json()) == LEGACY_RUNNING_KEYS | {"submitted_at"}

    def test__legacy_job_metadata_adds_no_fields(self, mock_modal, client: TestClient):
        job_id = "legacy-job"
        mock_modal["dicts"][JOB_METADATA_DICT_NAME] = {
            job_id: {
                "resolved_app_name": "legacy-worker",
                "policyengine_bundle": {"model_version": "1.500.0"},
                "observability_id": None,
            }
        }
        mock_modal["func"].call_for(job_id).running = True

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 202
        assert set(response.json()) == LEGACY_RUNNING_KEYS

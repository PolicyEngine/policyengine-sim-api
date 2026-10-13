"""Tests for client-visible job stage fields."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from policyengine_simulation_contract.gateway_models import JobStatusResponse
from policyengine_simulation_contract.job_progress import (
    JOB_PROGRESS_CAPABILITY_VERSION,
    RUNNING_PROGRESS_CAP,
    STAGE_PROGRESS_FLOOR,
    JobProgressRecord,
    JobSegmentProgress,
    JobStage,
    job_progress_fields,
    supported_job_progress_capability,
)
from policyengine_simulation_contract.job_progress_state import (
    JOB_PROGRESS_DICT_NAME,
    get_job_progress,
    put_job_progress,
)

SUBMITTED = "2026-10-13T12:00:00+00:00"
STARTED = datetime(2026, 10, 13, 12, 0, 30, tzinfo=UTC)
STAGE_STARTED = datetime(2026, 10, 13, 12, 1, 0, tzinfo=UTC)
FINISHED = datetime(2026, 10, 13, 12, 4, 0, tzinfo=UTC)


def _record(**overrides) -> JobProgressRecord:
    values = {
        "stage": JobStage.REFORM,
        "started_at": STARTED,
        "stage_started_at": STAGE_STARTED,
        "stage_durations": {JobStage.LOADING_DATA: 12.5, JobStage.BASELINE: 17.5},
    }
    return JobProgressRecord(**{**values, **overrides})


class TestJobProgressFieldsWithoutStageReporting:
    def test__legacy_job_adds_no_fields(self):
        assert (
            job_progress_fields(
                status="running",
                submitted_at=None,
                record=None,
                reports_stages=False,
            )
            == {}
        )

    def test__only_submission_time_is_reported(self):
        fields = job_progress_fields(
            status="running",
            submitted_at=SUBMITTED,
            record=_record(),
            reports_stages=False,
        )

        assert fields == {"submitted_at": "2026-10-13T12:00:00Z"}

    def test__malformed_submission_time_is_dropped(self):
        assert (
            job_progress_fields(
                status="running",
                submitted_at="not a time",
                record=None,
                reports_stages=False,
            )
            == {}
        )


class TestJobProgressFieldsWithStageReporting:
    def test__running_job_without_record_is_queued(self):
        fields = job_progress_fields(
            status="running",
            submitted_at=SUBMITTED,
            record=None,
            reports_stages=True,
        )

        assert fields == {
            "submitted_at": "2026-10-13T12:00:00Z",
            "stage": "queued",
            "progress": 0,
        }

    def test__running_job_reports_stage_floor_and_timing(self):
        fields = job_progress_fields(
            status="running",
            submitted_at=SUBMITTED,
            record=_record(),
            reports_stages=True,
        )

        assert fields == {
            "submitted_at": "2026-10-13T12:00:00Z",
            "started_at": "2026-10-13T12:00:30Z",
            "stage": "reform",
            "stage_started_at": "2026-10-13T12:01:00Z",
            "progress": STAGE_PROGRESS_FLOOR[JobStage.REFORM],
            "stage_durations": {"loading_data": 12.5, "baseline": 17.5},
        }

    @pytest.mark.parametrize(
        ("completed", "expected"),
        [(0, 15), (10, 50), (19, 81), (20, 85)],
    )
    def test__segmented_run_advances_with_completed_segments(self, completed, expected):
        record = _record(
            stage=JobStage.SIMULATING,
            segments=JobSegmentProgress(completed=completed, total=20),
        )

        fields = job_progress_fields(
            status="running",
            submitted_at=SUBMITTED,
            record=record,
            reports_stages=True,
        )

        assert fields["stage"] == "simulating"
        assert fields["progress"] == expected
        assert fields["segments"] == {"completed": completed, "total": 20}

    def test__running_progress_never_reaches_100(self):
        assert all(
            floor <= RUNNING_PROGRESS_CAP for floor in STAGE_PROGRESS_FLOOR.values()
        )
        record = _record(stage=JobStage.OUTPUTS)

        fields = job_progress_fields(
            status="running",
            submitted_at=SUBMITTED,
            record=record,
            reports_stages=True,
        )

        assert fields["progress"] < 100

    def test__complete_job_reports_100_and_no_current_stage(self):
        record = _record(stage=JobStage.OUTPUTS, finished_at=FINISHED)

        fields = job_progress_fields(
            status="complete",
            submitted_at=SUBMITTED,
            record=record,
            reports_stages=True,
        )

        assert fields["progress"] == 100
        assert fields["finished_at"] == "2026-10-13T12:04:00Z"
        assert "stage" not in fields
        assert "stage_started_at" not in fields

    def test__complete_job_without_record_still_reports_100(self):
        fields = job_progress_fields(
            status="complete",
            submitted_at=SUBMITTED,
            record=None,
            reports_stages=True,
        )

        assert fields == {"submitted_at": "2026-10-13T12:00:00Z", "progress": 100}

    def test__failed_job_reports_the_stage_it_stopped_in(self):
        record = _record(finished_at=FINISHED)

        fields = job_progress_fields(
            status="failed",
            submitted_at=SUBMITTED,
            record=record,
            reports_stages=True,
        )

        assert fields["stage"] == "reform"
        assert fields["finished_at"] == "2026-10-13T12:04:00Z"
        assert "progress" not in fields

    def test__failed_job_without_record_reports_no_stage(self):
        fields = job_progress_fields(
            status="failed",
            submitted_at=SUBMITTED,
            record=None,
            reports_stages=True,
        )

        assert fields == {"submitted_at": "2026-10-13T12:00:00Z"}

    @pytest.mark.parametrize("status", ["running", "complete", "failed"])
    def test__fields_are_valid_public_status_fields(self, status):
        record = _record(
            stage=JobStage.SIMULATING,
            segments=JobSegmentProgress(completed=3, total=20),
            finished_at=FINISHED if status != "running" else None,
        )
        fields = job_progress_fields(
            status=status,
            submitted_at=SUBMITTED,
            record=record,
            reports_stages=True,
        )

        response = JobStatusResponse(status=status, **fields)

        assert response.model_dump(mode="json", exclude_none=True) == {
            "status": status,
            **fields,
        }


class TestSupportedJobProgressCapability:
    def test__current_version_is_supported(self):
        assert (
            supported_job_progress_capability(JOB_PROGRESS_CAPABILITY_VERSION)
            == JOB_PROGRESS_CAPABILITY_VERSION
        )

    @pytest.mark.parametrize("value", [None, True, "1", 0, 2, 1.0])
    def test__other_values_are_unsupported(self, value):
        assert supported_job_progress_capability(value) is None


class TestJobProgressModels:
    def test__completed_segments_cannot_exceed_total(self):
        with pytest.raises(ValidationError):
            JobSegmentProgress(completed=21, total=20)

    def test__record_requires_timezone_aware_times(self):
        with pytest.raises(ValidationError):
            _record(started_at=datetime(2026, 10, 13, 12, 0, 30))


class TestJobProgressState:
    def test__record_round_trips_through_the_store(self, mock_modal):
        record = _record(segments=JobSegmentProgress(completed=2, total=20))

        put_job_progress("fc-job-123", record)

        assert get_job_progress("fc-job-123") == record
        stored = mock_modal["dicts"][JOB_PROGRESS_DICT_NAME]["fc-job-123"]
        assert stored["stage"] == "reform"

    def test__missing_record_is_none(self, mock_modal):
        assert get_job_progress("fc-unknown") is None

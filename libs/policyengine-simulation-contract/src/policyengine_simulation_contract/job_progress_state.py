"""Modal Dict storage for annual job stage records."""

from __future__ import annotations

import modal

from policyengine_simulation_contract.job_progress import JobProgressRecord

JOB_PROGRESS_DICT_NAME = "simulation-api-job-progress"


def _job_progress_store() -> modal.Dict:
    return modal.Dict.from_name(JOB_PROGRESS_DICT_NAME, create_if_missing=True)


def get_job_progress(job_id: str) -> JobProgressRecord | None:
    payload = _job_progress_store().get(job_id)
    if payload is None:
        return None
    return JobProgressRecord.model_validate(payload)


def put_job_progress(job_id: str, record: JobProgressRecord) -> None:
    _job_progress_store()[job_id] = record.model_dump(mode="json")

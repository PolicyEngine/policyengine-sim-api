"""Comparison of completed Stage 12 and production report results."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ResultComparisonStatus,
    Stage12InvocationContext,
)

from policyengine_simulation_executor.stage12_artifacts import Stage12ArtifactStore
from policyengine_simulation_executor.stage12_result_comparison import compare_results

from .dependencies import ChildInvoker, ComparisonStore

PRODUCTION_RESULT_WAIT_TIMEOUT_SECONDS = 900
logger = logging.getLogger(__name__)


def compare_completed_report(
    *,
    parent: ComparisonReportRecord,
    aggregate: dict[str, Any],
    context: Stage12InvocationContext,
    store: ComparisonStore,
    artifacts: Stage12ArtifactStore,
    invoker: ChildInvoker,
) -> None:
    """Persist an isolated production/Stage 12 comparison after aggregation."""

    production_job_id = context.production_function_call_id
    if production_job_id is None:
        return
    try:
        comparing_at = datetime.now(UTC)
        comparing = store.replace_report_result_comparison(
            parent.model_copy(
                update={
                    "comparison_status": ResultComparisonStatus.RUNNING,
                    "comparison_output_uri": None,
                    "comparison_output_sha256": None,
                    "comparison_schema_version": None,
                    "comparison_completed_at": None,
                    "comparison_error_code": None,
                    "comparison_error_summary": None,
                    "updated_at": comparing_at,
                }
            )
        )
        production_result = invoker.restore(production_job_id).get(
            timeout=PRODUCTION_RESULT_WAIT_TIMEOUT_SECONDS
        )
        comparison = compare_results(
            evaluation_id=parent.evaluation_id,
            production_job_id=production_job_id,
            production_result=production_result,
            stage12_result=aggregate.get("result"),
        )
        comparison_artifact = artifacts.write_comparison(
            prefix=context.artifact_prefix,
            payload=comparison.model_dump(mode="json"),
        )
        comparison_status = (
            ResultComparisonStatus.MATCHED
            if comparison.status == "matched"
            else ResultComparisonStatus.DIFFERENT
        )
        store.replace_report_result_comparison(
            comparing.model_copy(
                update={
                    "comparison_status": comparison_status,
                    "comparison_output_uri": comparison_artifact.uri,
                    "comparison_output_sha256": comparison_artifact.content_sha256,
                    "comparison_schema_version": comparison.schema_version,
                    "comparison_completed_at": comparison.compared_at,
                    "updated_at": comparison.compared_at,
                }
            )
        )
        logger.info(
            "stage12_result_comparison_completed",
            extra={
                "comparison_run_id": str(parent.evaluation_id),
                "production_job_id": production_job_id,
                "comparison_status": comparison_status.value,
                "difference_count": comparison.difference_count,
                "production_result_sha256": comparison.production_result_sha256,
                "stage12_result_sha256": comparison.stage12_result_sha256,
            },
        )
    # Result retrieval, validation, artifact storage, and persistence are all
    # deliberately isolated from the successful Stage 12 calculation.
    except Exception as error:  # noqa: BLE001
        failed_at = datetime.now(UTC)
        try:
            latest = store.get_report(parent.evaluation_id)
            if latest.comparison_status not in {
                ResultComparisonStatus.MATCHED,
                ResultComparisonStatus.DIFFERENT,
            }:
                store.replace_report_result_comparison(
                    latest.model_copy(
                        update={
                            "comparison_status": ResultComparisonStatus.FAILED,
                            "comparison_output_uri": None,
                            "comparison_output_sha256": None,
                            "comparison_schema_version": None,
                            "comparison_completed_at": failed_at,
                            "comparison_error_code": "result_comparison_failed",
                            "comparison_error_summary": type(error).__name__,
                            "updated_at": failed_at,
                        }
                    )
                )
        except Exception:
            logger.exception(
                "stage12_result_comparison_state_update_failed",
                extra={
                    "comparison_run_id": str(parent.evaluation_id),
                    "production_job_id": production_job_id,
                },
            )
        logger.error(
            "stage12_result_comparison_failed",
            extra={
                "comparison_run_id": str(parent.evaluation_id),
                "production_job_id": production_job_id,
                "error_type": type(error).__name__,
            },
        )

"""HTTP implementation of the temporary Stage 12 persistence contract."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportPersistenceResult,
    ComparisonReportRecord,
    ComparisonSimulationPersistenceResult,
    ComparisonSimulationRecord,
)
from pydantic import ValidationError

TokenProvider = Callable[[], str]


class Stage12PersistenceClient:
    """Call the API-owned persistence service without direct database access."""

    def __init__(
        self,
        base_url: str,
        *,
        token_provider: TokenProvider,
        http_client: httpx.Client | None = None,
    ) -> None:
        normalized_url = base_url.rstrip("/")
        parsed = urlparse(normalized_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Stage 12 persistence API must be an absolute HTTPS URL")
        self._base_url = normalized_url
        self._token_provider = token_provider
        self._http_client = http_client or httpx.Client(timeout=30.0)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._http_client.request(
                method,
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._token_provider()}"},
                json=json,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise RuntimeError("Stage 12 persistence API is unavailable") from error
        if response.status_code == 404:
            raise LookupError("Stage 12 comparison record does not exist")
        if response.status_code in {400, 409, 422}:
            raise ValueError("Stage 12 persistence request conflicts with stored state")
        if not response.is_success:
            raise RuntimeError("Stage 12 persistence API operation failed")
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError(
                "Stage 12 persistence API returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise RuntimeError(  # noqa: TRY004 - invalid remote protocol response
                "Stage 12 persistence API returned an invalid response"
            )
        return payload

    @staticmethod
    def _report(payload: dict[str, Any]) -> ComparisonReportRecord:
        try:
            return ComparisonReportRecord.model_validate(payload.get("record"))
        except ValidationError as error:
            raise RuntimeError(
                "Stage 12 persistence API returned an invalid report"
            ) from error

    @staticmethod
    def _simulation(payload: dict[str, Any]) -> ComparisonSimulationRecord:
        try:
            return ComparisonSimulationRecord.model_validate(payload.get("record"))
        except ValidationError as error:
            raise RuntimeError(
                "Stage 12 persistence API returned an invalid simulation"
            ) from error

    @staticmethod
    def _created(payload: dict[str, Any]) -> bool:
        created = payload.get("created")
        if not isinstance(created, bool):
            raise RuntimeError(  # noqa: TRY004 - invalid remote protocol response
                "Stage 12 persistence API returned an invalid response"
            )
        return created

    def create_or_resolve_report(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportPersistenceResult:
        payload = self._request(
            "POST",
            "/internal/stage12/comparison-runs/reports/resolve",
            json=record.model_dump(mode="json"),
        )
        return ComparisonReportPersistenceResult(
            record=self._report(payload),
            created=self._created(payload),
        )

    def get_report(self, evaluation_id: UUID) -> ComparisonReportRecord:
        payload = self._request(
            "GET",
            f"/internal/stage12/comparison-runs/reports/{evaluation_id}",
        )
        return self._report(payload)

    def list_simulations(
        self,
        evaluation_id: UUID,
    ) -> tuple[ComparisonSimulationRecord, ...]:
        payload = self._request(
            "GET",
            f"/internal/stage12/comparison-runs/reports/{evaluation_id}/simulations",
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise RuntimeError(  # noqa: TRY004 - invalid remote protocol response
                "Stage 12 persistence API returned an invalid response"
            )
        try:
            return tuple(
                ComparisonSimulationRecord.model_validate(item) for item in items
            )
        except ValidationError as error:
            raise RuntimeError(
                "Stage 12 persistence API returned invalid simulations"
            ) from error

    def replace_report(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportRecord:
        payload = self._request(
            "PUT",
            (
                "/internal/stage12/comparison-runs/reports/"
                f"{record.evaluation_id}/lifecycle"
            ),
            json=record.model_dump(mode="json"),
        )
        return self._report(payload)

    def replace_report_result_comparison(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportRecord:
        payload = self._request(
            "PUT",
            (
                "/internal/stage12/comparison-runs/reports/"
                f"{record.evaluation_id}/comparison"
            ),
            json=record.model_dump(mode="json"),
        )
        return self._report(payload)

    def create_or_resolve_simulation(
        self,
        record: ComparisonSimulationRecord,
    ) -> ComparisonSimulationPersistenceResult:
        payload = self._request(
            "POST",
            "/internal/stage12/comparison-runs/simulations/resolve",
            json=record.model_dump(mode="json"),
        )
        return ComparisonSimulationPersistenceResult(
            record=self._simulation(payload),
            created=self._created(payload),
        )

    def get_simulation(
        self,
        simulation_execution_id: UUID,
    ) -> ComparisonSimulationRecord:
        payload = self._request(
            "GET",
            (
                "/internal/stage12/comparison-runs/simulations/"
                f"{simulation_execution_id}"
            ),
        )
        return self._simulation(payload)

    def replace_simulation(
        self,
        record: ComparisonSimulationRecord,
    ) -> ComparisonSimulationRecord:
        payload = self._request(
            "PUT",
            (
                "/internal/stage12/comparison-runs/simulations/"
                f"{record.simulation_execution_id}/lifecycle"
            ),
            json=record.model_dump(mode="json"),
        )
        return self._simulation(payload)

    def attach_simulation_invocation(
        self,
        simulation_execution_id: UUID,
        *,
        expected_placeholder: str,
        modal_invocation_id: str,
        updated_at: datetime,
    ) -> ComparisonSimulationRecord:
        payload = self._request(
            "POST",
            (
                "/internal/stage12/comparison-runs/simulations/"
                f"{simulation_execution_id}/invocation"
            ),
            json={
                "expected_placeholder": expected_placeholder,
                "modal_invocation_id": modal_invocation_id,
                "updated_at": updated_at.isoformat(),
            },
        )
        return self._simulation(payload)

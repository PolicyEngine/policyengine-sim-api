"""The route on which sim's call-report Modal function reports that a call ended.

``POST /internal/modal-calls/ended`` (``CALL_ENDED_PATH``), not in the public
OpenAPI document. The request carries the Modal identity token of the
``report_call_ended`` function as a bearer token, and a ``CallEndedReport``
naming the call and its runs, never a result. The token is checked first, so
a caller without it learns nothing about the body. Then the reported call's
outcome is recorded, if Modal has one (``RunRecorder.record_reported_call``).

Answers, each a ``CallReportAnswer``:

- 200: ``recorded`` or ``already_recorded``; a repeated report changes
  nothing.
- 503 with ``Retry-After``: a status in ``RETRY_LATER_STATUSES``; the sender
  reports again after a wait, and the safety check covers a call whose
  sender gives up.
- 404 ``unknown_runs`` and 409 ``other_call``: the report cannot be recorded;
  the sender stops.
- 401: the token is missing or refused; 422: the body is not a report.

Only the revision that serves traffic receives these requests, since the
sender posts to sim's main URL.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol

from fastapi import Request, status
from fastapi.responses import JSONResponse
from policyengine_simulation_contract.call_reports import (
    CALL_ENDED_PATH,
    CallEndedReport,
    CallReportAnswer,
    CallReportStatus,
)
from pydantic import ValidationError

from policyengine_simulation_entry.modal_identity import (
    ModalIdentityError,
    ModalKeysUnavailableError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from policyengine_simulation_entry.modal_identity import ModalIdentity

logger = logging.getLogger(__name__)

# How long the sender waits before reporting again, in seconds.
RETRY_AFTER_SECONDS = 5
_REFUSED_STATUSES = {
    CallReportStatus.UNKNOWN_RUNS: status.HTTP_404_NOT_FOUND,
    CallReportStatus.OTHER_CALL: status.HTTP_409_CONFLICT,
}
_BEARER_PREFIX = "bearer "


class CallReportRecorder(Protocol):
    """Record a reported call's outcome (``RunRecorder``)."""

    async def record_reported_call(self, report: CallEndedReport) -> CallReportStatus:
        """Record the call's outcome if Modal has one, and say what was done."""
        ...


class ModalCallerVerifier(Protocol):
    """Check a Modal identity token (``ModalIdentityVerifier``)."""

    def verify(self, token: str) -> ModalIdentity:
        """Return the token's identity, or raise ModalIdentityError."""
        ...


def _answer(report_status: CallReportStatus) -> JSONResponse:
    answer = CallReportAnswer(status=report_status)
    if answer.retry_later:
        return JSONResponse(
            answer.model_dump(mode="json"),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    return JSONResponse(
        answer.model_dump(mode="json"),
        status_code=_REFUSED_STATUSES.get(report_status, status.HTTP_200_OK),
    )


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"detail": "A Modal identity token of the call-report function is required."},
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith(_BEARER_PREFIX):
        return None
    return header[len(_BEARER_PREFIX) :].strip() or None


async def _caller(
    request: Request, verifier: ModalCallerVerifier
) -> ModalIdentity | JSONResponse:
    """Return the verified Modal caller, or the answer that refuses or defers it."""
    token = _bearer_token(request)
    if token is None:
        return _unauthorized()
    try:
        return await asyncio.to_thread(verifier.verify, token)
    except ModalKeysUnavailableError:
        logger.warning("call_report_modal_keys_unavailable")
        return _answer(CallReportStatus.MODAL_UNREACHABLE)
    except ModalIdentityError as error:
        logger.warning("call_report_refused", extra={"reason": str(error)})
        return _unauthorized()
    # Anything else is this instance's own problem, not a refusal: the sender
    # reports again after the wait.
    except Exception:
        logger.exception("call_report_token_not_checked")
        return _answer(CallReportStatus.MODAL_UNREACHABLE)


def register_call_report_route(
    app: FastAPI,
    *,
    verifier: ModalCallerVerifier | None,
    recorder: CallReportRecorder | None,
) -> None:
    """Add the call-ended route; without both seams it answers 503."""

    @app.post(CALL_ENDED_PATH, include_in_schema=False)
    async def report_call_ended(request: Request) -> JSONResponse:
        if verifier is None or recorder is None:
            return _answer(CallReportStatus.NOT_CONFIGURED)
        identity = await _caller(request, verifier)
        if isinstance(identity, JSONResponse):
            return identity
        try:
            report = CallEndedReport.model_validate_json(await request.body())
        except ValidationError as error:
            return JSONResponse(
                {"detail": error.errors(include_url=False, include_input=False)},
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        report_status = await recorder.record_reported_call(report)
        logger.info(
            "call_report_answered",
            extra={
                "modal_call_id": report.modal_call_id,
                "call_report_status": report_status.value,
                "modal_app_name": identity.app_name,
                "modal_container_id": identity.container_id,
            },
        )
        return _answer(report_status)

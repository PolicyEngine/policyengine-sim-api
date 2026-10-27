"""Report to the Simulation Entrypoint that a v2 Modal call ended.

Interface for the v2 functions' last step (the economy coordinator, item
2.01, and the household functions, item 2.03). Wrap the work of a top-level
function whose call sim started::

    with reporting_call_end(
        modal_call_id=modal.current_function_call_id(),
        simulation_run_ids=run_ids,  # or report_run_id=report_run_id
        start_report=report_call_ended.spawn,
    ):
        result = calculate(...)
    return result

When the block ends, normally or by raising an ``Exception``, the report is
started as a separate call of the ``report_call_ended`` Modal function and
the block's own result or exception goes on unchanged. Starting it never
raises. A signal sends nothing: Modal delivers pre-emption as
``KeyboardInterrupt``, re-runs the call under the same ID, and the re-run
reports. Segment calls report to their coordinator, not to sim.

Why a separate call: this function's own output exists only once it has
returned, and sim asks Modal for the outcome itself, without waiting, so a
report sent from inside the call would always find it still running.
``report_call_ended`` runs ``send_call_ended_report_from_container``: it
posts the ``CallEndedReport`` (the call ID and run IDs, never a result) to
``SIMULATION_ENTRY_URL`` + ``CALL_ENDED_PATH`` with the container's Modal
identity token, and reports again after a wait while sim answers 503 or
cannot be reached, up to ``CALL_REPORT_ATTEMPTS`` times. A report that never
arrives is recorded by sim's safety check within about ten minutes.

This module is v2 code only: the main path's ``run_simulation`` does not use
it.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_contract.call_reports import (
    CALL_ENDED_PATH,
    MODAL_IDENTITY_ENVIRONMENT_VARIABLE,
    CallEndedReport,
)
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from uuid import UUID

logger = logging.getLogger(__name__)

# The Simulation Entrypoint's main URL, which the deploy bakes into the
# report_call_ended function's environment.
ENTRY_URL_VARIABLE = "SIMULATION_ENTRY_URL"
CALL_REPORT_ATTEMPTS = 6
FIRST_WAIT_SECONDS = 1.0
LONGEST_WAIT_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 15.0
# sim answers these when it cannot record the outcome yet.
_RETRY_LATER_HTTP_STATUSES = frozenset({429, 502, 503, 504})


class ReportDelivery(StrEnum):
    """What sending one report came to."""

    # sim recorded the outcome, or already had it.
    DELIVERED = "delivered"
    # sim refused the report; sending it again would not help.
    REFUSED = "refused"
    # sim could not record it after every attempt; the safety check will.
    GAVE_UP = "gave_up"
    # The container has no URL for sim or no identity token.
    NOT_CONFIGURED = "not_configured"


class Poster(Protocol):
    """Send one HTTP POST; return the status, headers and body."""

    def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, Mapping[str, str], bytes]:
        """Post ``body`` to ``url``; raise OSError when nothing came back."""
        ...


def post_with_urllib(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> tuple[int, Mapping[str, str], bytes]:
    """Post with the standard library; an HTTP error status is an answer."""
    request = urllib.request.Request(  # noqa: S310  # an https URL the deploy sets
        url, data=body, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # as above
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def _wait_seconds(attempt: int, headers: Mapping[str, str]) -> float:
    retry_after = {name.lower(): value for name, value in headers.items()}.get(
        "retry-after", ""
    )
    backoff = min(FIRST_WAIT_SECONDS * 2**attempt, LONGEST_WAIT_SECONDS)
    if retry_after.isdigit():
        return min(max(float(retry_after), backoff), LONGEST_WAIT_SECONDS)
    return backoff


def _answer_status(body: bytes) -> str:
    try:
        answer = json.loads(body)
    except ValueError:
        return ""
    return str(answer.get("status", "")) if isinstance(answer, dict) else ""


def send_call_ended_report(
    report: CallEndedReport,
    *,
    entry_url: str,
    identity_token: str,
    post: Poster = post_with_urllib,
    sleep: Callable[[float], None] = time.sleep,
) -> ReportDelivery:
    """Post ``report`` to sim, reporting again while sim cannot record it yet.

    Never raises an ``Exception``: a report that cannot be delivered is
    logged, and sim's safety check records the call instead.
    """
    if not entry_url or not identity_token:
        logger.warning(
            "call_report_not_configured",
            extra={"modal_call_id": report.modal_call_id},
        )
        return ReportDelivery.NOT_CONFIGURED
    url = entry_url.rstrip("/") + CALL_ENDED_PATH
    body = report.model_dump_json().encode()
    headers = {
        "Authorization": f"Bearer {identity_token}",
        "Content-Type": "application/json",
    }
    answer_headers: Mapping[str, str] = {}
    for attempt in range(CALL_REPORT_ATTEMPTS):
        if attempt:
            sleep(_wait_seconds(attempt - 1, answer_headers))
        try:
            status, answer_headers, answer_body = post(
                url, body, headers, REQUEST_TIMEOUT_SECONDS
            )
        except OSError as error:  # no answer: unreachable, reset or timed out
            answer_headers = {}
            logger.warning(
                "call_report_not_answered",
                extra={
                    "modal_call_id": report.modal_call_id,
                    "attempt": attempt + 1,
                    "error_type": type(error).__name__,
                },
            )
            continue
        fields = {
            "modal_call_id": report.modal_call_id,
            "attempt": attempt + 1,
            "http_status": status,
            "call_report_status": _answer_status(answer_body),
        }
        if status < 300:  # noqa: PLR2004  # any 2xx answer
            logger.info("call_report_delivered", extra=fields)
            return ReportDelivery.DELIVERED
        if status not in _RETRY_LATER_HTTP_STATUSES:
            logger.warning("call_report_refused", extra=fields)
            return ReportDelivery.REFUSED
        logger.info("call_report_not_recorded_yet", extra=fields)
    logger.warning(
        "call_report_given_up",
        extra={"modal_call_id": report.modal_call_id, "attempts": CALL_REPORT_ATTEMPTS},
    )
    return ReportDelivery.GAVE_UP


def send_call_ended_report_from_container(
    report: Mapping[str, object],
    *,
    post: Poster = post_with_urllib,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Run in ``report_call_ended``: send ``report`` with this container's settings.

    Return the ``ReportDelivery`` value. A malformed report is refused.
    """
    try:
        call_report = CallEndedReport.model_validate(report)
    except ValidationError:
        logger.warning("call_report_malformed")
        return ReportDelivery.REFUSED.value
    return send_call_ended_report(
        call_report,
        entry_url=os.environ.get(ENTRY_URL_VARIABLE, ""),
        identity_token=os.environ.get(MODAL_IDENTITY_ENVIRONMENT_VARIABLE, ""),
        post=post,
        sleep=sleep,
    ).value


def _start_report(
    report: CallEndedReport, start_report: Callable[[dict[str, object]], object]
) -> None:
    try:
        start_report(report.model_dump(mode="json"))
    except Exception as error:  # a failed report must never fail the calculation
        logger.exception(
            "call_report_not_started",
            extra={
                "modal_call_id": report.modal_call_id,
                "error_type": type(error).__name__,
            },
        )


@contextmanager
def reporting_call_end(
    *,
    modal_call_id: str | None,
    start_report: Callable[[dict[str, object]], object],
    simulation_run_ids: Sequence[UUID] | None = None,
    report_run_id: UUID | None = None,
) -> Iterator[None]:
    """Report the call's end when the block returns or raises an Exception.

    ``start_report`` starts the ``report_call_ended`` function (its
    ``spawn``). Without a call ID (outside Modal), or with run IDs the report
    cannot carry, nothing is reported.
    """
    report: CallEndedReport | None = None
    try:
        if modal_call_id is not None:
            report = CallEndedReport(
                modal_call_id=modal_call_id,
                simulation_run_ids=(
                    None if simulation_run_ids is None else tuple(simulation_run_ids)
                ),
                report_run_id=report_run_id,
            )
    except ValidationError:
        logger.warning("call_report_malformed", extra={"modal_call_id": modal_call_id})
    if report is None:
        yield
        return
    try:
        yield
    except Exception:
        _start_report(report, start_report)
        raise
    _start_report(report, start_report)

"""Refused ``POST /reports`` bodies, located as before the household report type.

``POST /reports`` takes an economy or a household report, told apart by
``report_type``. Pydantic puts the matched type into every refused field's
location (``["body", "economy", "year"]``) and reports a missing or unknown
``report_type`` at the body itself. The Public API passes the locations to
its callers, so the report route answers them as it did when it took only
economy reports: without the type, and with ``report_type`` named. A body
with no ``report_type`` is refused for that alone, before its other fields
are checked, since its type decides which fields it must have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, get_args

from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from policyengine_simulation_entry.household_report_api_models import ReportRequest

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from fastapi import Request, Response

# The values of ``report_type``, which Pydantic puts into the locations: one
# per request model the route's body takes.
REPORT_TYPES: Final = tuple(
    report_type
    for model in get_args(get_args(ReportRequest)[0])
    for report_type in get_args(model.model_fields["report_type"].annotation)
)


def _expected(report_types: Sequence[str]) -> str:
    """Return the types as Pydantic lists a literal's values: 'a', 'b' or 'c'."""
    quoted = [f"'{report_type}'" for report_type in report_types]
    return " or ".join(
        [", ".join(quoted[:-1]), quoted[-1]] if len(quoted) > 1 else quoted
    )


_EXPECTED_TYPES: Final = _expected(REPORT_TYPES)


def report_request_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Return the refusals of a report body with the request type left out.

    A field's location loses the type Pydantic matched; a body with no
    ``report_type`` is refused as missing it, and one with an unknown type as
    a value outside the types.
    """
    return [_located(dict(error)) for error in errors]


def _located(error: dict[str, Any]) -> dict[str, Any]:
    location = tuple(error.get("loc", ()))
    kind = error.get("type")
    if location == ("body",) and kind == "union_tag_not_found":
        return {
            "type": "missing",
            "loc": ("body", "report_type"),
            "msg": "Field required",
            "input": error.get("input"),
        }
    if location == ("body",) and kind == "union_tag_invalid":
        body = error.get("input")
        return {
            "type": "literal_error",
            "loc": ("body", "report_type"),
            "msg": f"Input should be {_EXPECTED_TYPES}",
            # The value sent, as JSON; the error's own tag is its text.
            "input": body.get("report_type") if isinstance(body, dict) else None,
            "ctx": {"expected": _EXPECTED_TYPES},
        }
    if len(location) >= 2 and location[0] == "body" and location[1] in REPORT_TYPES:  # noqa: PLR2004  # the body, then the type
        return {**error, "loc": (location[0], *location[2:])}
    return error


class ReportRequestRoute(APIRoute):
    """The report route, whose refused bodies are located as an economy report's."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Return the route's handler, relocating a refused body's errors."""
        handle = super().get_route_handler()

        async def handle_report(request: Request) -> Response:
            try:
                return await handle(request)
            except RequestValidationError as error:
                raise RequestValidationError(
                    report_request_errors(error.errors()), body=error.body
                ) from None

        return handle_report

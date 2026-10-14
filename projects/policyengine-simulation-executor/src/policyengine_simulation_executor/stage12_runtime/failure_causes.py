"""Bounded, redacted failure causes for durable Stage 12 records.

A Stage 12 failure record names the exception type, the stage the process
was in (or had last finished), the simulation role where one applies, and a
redacted message. Without the message an operator cannot tell a database
session limit from a validation defect, because both surface as a generic
exception class.

The summary stored in ``error_summary`` and the ``Stage12FailureCause``
recorded to telemetry are redacted. Redaction removes credentials embedded in
URLs, the query strings of URLs and paths, secret-looking key/value pairs
(including quoted JSON and dictionary keys), ``Authorization`` and cookie
header values, bearer and other known access tokens, the values of this
process's secret environment variables, Pydantic validation input values, SQL
statement parameters and the row values that database constraint errors
echo. Redaction reads a bounded prefix of the message, and the summary fits
the contract's ``ErrorSummary`` length.

Redaction covers only this summary and the cause recorded with it. The
exception events that ``runtime.span`` and ``runtime.operation`` scopes
export when an exception leaves them, and the original-failure record that
``failure_recording`` logs before a failure write, carry the original
message, filtered only by the observability package's own redaction; this
module does not change them.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from policyengine_simulation_contract.stage12_execution import ErrorSummary
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import StatementError

from policyengine_simulation_executor.hf_access_validation import HF_RUNTIME_ENV_NAME

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_observability.stages import Stage, StagePlan

# The longest ``error_summary`` the contract accepts.
MAX_FAILURE_SUMMARY_LENGTH: int = TypeAdapter(ErrorSummary).json_schema()["maxLength"]
# Redaction reads at most this much of a message. The cap bounds the cost of
# redacting an arbitrarily long message and is far above the summary length.
MAX_REDACTED_MESSAGE_LENGTH = 4_096
REDACTED = "[REDACTED]"
TRUNCATION_MARKER = "..."
SECRET_ENVIRONMENT_VARIABLES = ("STAGE12_DATABASE_URL", HF_RUNTIME_ENV_NAME)
MIN_SECRET_VALUE_LENGTH = 8
logger = logging.getLogger(__name__)

_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    re.DOTALL,
)
# A pattern that could start anywhere in a run of word characters only starts
# where the run starts, so matching stays linear in the length of the message.
_URL_SCHEME = r"(?<![a-zA-Z0-9+.-])[a-zA-Z][a-zA-Z0-9+.-]*://"
# User information runs to the last "@" before the path, so a password that
# contains "@" is removed whole.
_URL_CREDENTIALS = re.compile(rf"(?P<scheme>{_URL_SCHEME})[^\s/'\"]+@")
# Every URL matches, with or without a query, so a failed match never
# rescans the rest of a long address.
_URL_ADDRESS = re.compile(
    rf"(?P<address>{_URL_SCHEME}[^\s?'\"]+)(?P<query>\?[^\s'\"]+)?"
)
# A query string without a scheme, such as on a signed object path.
_QUERY_STRING = re.compile(r"\?(?=[\w.~%+-]++=)[^\s'\"]+")
# A quoted value may contain escaped quotes. A value whose closing quote was
# cut off runs to the end of the message.
_QUOTED_VALUE = r"\"(?:[^\"\\]|\\.)*(?:\"|\Z)|'(?:[^'\\]|\\.)*(?:'|\Z)"
# An unquoted header value can contain spaces, as in "Basic <credentials>" or
# a Digest parameter list, so it runs to the end of the line.
_CREDENTIAL_HEADER = re.compile(
    r"(?<![\w-])(?P<key>[\w-]*(?:authorization|cookie))"
    r"(?P<separator>[\"']?\s*[=:]\s*)"
    rf"(?P<value>{_QUOTED_VALUE}|[^\r\n]*)",
    re.IGNORECASE | re.DOTALL,
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_KEY = (
    r"[\w-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"  # noqa: S105  # a pattern for secret-looking key names, not a secret
    r"|private[_-]?key|credentials?|signature|sig)"
)
_SECRET_ASSIGNMENT = re.compile(
    rf"(?<![\w-])(?P<key>{_SECRET_KEY})(?P<separator>[\"']?\s*[=:]\s*)"
    rf"(?P<value>{re.escape(REDACTED)}|{_QUOTED_VALUE}|[^\s,;)\]}}]+)",
    re.IGNORECASE | re.DOTALL,
)
_KNOWN_TOKEN_FORMATS = re.compile(
    r"\b(?:hf_[A-Za-z0-9]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|ya29\.[0-9A-Za-z_-]+"
    r"|(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
_PYDANTIC_INPUT_VALUE = re.compile(
    r"input_value=.*?(?:(?=, input_type=)|\Z)",
    re.DOTALL,
)
_SQL_PARAMETERS = re.compile(
    r"\[parameters: .*?(?:\](?=\s*(?:\(Background|\Z))|\Z)",
    re.DOTALL,
)
# PostgreSQL constraint violations echo the row's values in their detail. A
# value can contain a line break, so redaction runs to the end of the message.
_CONSTRAINT_KEY_VALUES = re.compile(
    r"\bKey \((?P<columns>[^()\r\n]*(?:\([^()\r\n]*\)[^()\r\n]*)*)\)=\(.*",
    re.DOTALL,
)
_FAILING_ROW_VALUES = re.compile(r"\bFailing row contains \(.*", re.DOTALL)
_SQLALCHEMY_BACKGROUND = re.compile(r"\(Background on this error at: [^)]*\)")
_WHITESPACE = re.compile(r"\s+")


class StageProgress:
    """Remember the last Stage 12 stage a process entered and whether it ended.

    A failure inside a stage is reported as ``in <stage>``. A failure between
    stages is reported as ``after <stage>``, naming the last stage that
    completed, so code between two spans is not attributed to either of them.
    """

    def __init__(self, *, role: str | None = None) -> None:
        """Start with no stage entered, attributing stages to ``role`` by default."""
        self.process_role = role
        self.stage_name: str | None = None
        self.stage_role: str | None = role
        self.stage_finished = False

    @contextmanager
    def stage(self, stage_name: str, *, role: str | None = None) -> Iterator[None]:
        """Record ``stage_name`` as the current stage while the block runs.

        The stage is marked finished only when the block exits without an
        exception; ``role`` defaults to the process role.
        """
        stage_role = role if role is not None else self.process_role
        self.stage_name = stage_name
        self.stage_role = stage_role
        self.stage_finished = False
        yield
        # An exception skips this, leaving the failing stage recorded.
        if self.stage_name == stage_name and self.stage_role == stage_role:
            self.stage_finished = True


@contextmanager
def tracked_stage(
    stage: Stage,
    attributes: Mapping[str, object] | None = None,
    *,
    stage_plan: StagePlan,
    progress: StageProgress,
    runtime: ObservabilityRuntime | None,
    operation: bool = False,
) -> Iterator[None]:
    """Run a block as one Stage 12 stage for telemetry and failure causes.

    The block runs inside the stage's span, or its operation when
    ``operation`` is true, and ``progress`` records the stage so that a
    failure names it. A ``simulation_role`` attribute sets the stage's role.
    """
    stage_name = stage_plan.name(stage)
    scope: AbstractContextManager[object]
    if runtime is None:
        scope = nullcontext()
    elif operation:
        scope = runtime.operation(stage_name, attributes=attributes)
    else:
        scope = runtime.span(stage_name, attributes=attributes)
    with progress.stage(stage_name, role=_simulation_role(attributes)), scope:
        yield


class Stage12FailureCause(Exception):
    """A redacted stand-in for a failure, safe to export to telemetry."""


def redact_failure_message(message: str) -> str:
    """Remove credentials, tokens and stored values from an exception message.

    Only the first ``MAX_REDACTED_MESSAGE_LENGTH`` characters are redacted and
    returned. The word that limit cuts is dropped, because part of a secret
    may no longer match its pattern.
    """
    truncated = len(message) > MAX_REDACTED_MESSAGE_LENGTH
    redacted = (
        _before_last_word(message[:MAX_REDACTED_MESSAGE_LENGTH])
        if truncated
        else message
    )
    redacted = _PRIVATE_KEY_BLOCK.sub(REDACTED, redacted)
    for secret in _secret_environment_values():
        redacted = redacted.replace(secret, REDACTED)
    redacted = _URL_CREDENTIALS.sub(rf"\g<scheme>{REDACTED}@", redacted)
    redacted = _URL_ADDRESS.sub(_without_url_query, redacted)
    redacted = _QUERY_STRING.sub(f"?{REDACTED}", redacted)
    redacted = _CREDENTIAL_HEADER.sub(rf"\g<key>\g<separator>{REDACTED}", redacted)
    redacted = _BEARER_TOKEN.sub(f"Bearer {REDACTED}", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(rf"\g<key>\g<separator>{REDACTED}", redacted)
    redacted = _KNOWN_TOKEN_FORMATS.sub(REDACTED, redacted)
    redacted = _PYDANTIC_INPUT_VALUE.sub(f"input_value={REDACTED}", redacted)
    redacted = _SQL_PARAMETERS.sub(f"[parameters: {REDACTED}]", redacted)
    redacted = _CONSTRAINT_KEY_VALUES.sub(rf"Key (\g<columns>)={REDACTED}", redacted)
    redacted = _FAILING_ROW_VALUES.sub(f"Failing row contains {REDACTED}", redacted)
    redacted = _SQLALCHEMY_BACKGROUND.sub("", redacted)
    redacted = _WHITESPACE.sub(" ", redacted).strip()
    return f"{redacted}{TRUNCATION_MARKER}" if truncated else redacted


def failure_summary(
    error: BaseException,
    *,
    progress: StageProgress | None = None,
) -> str:
    """Return a redacted ``<type> in <stage> (<role>): <message>`` summary."""
    error_type = type(error).__name__
    try:
        heading = f"{error_type}{_location(progress)}"
        message = redact_failure_message(_failure_message(error))
        return _bounded(f"{heading}: {message}" if message else heading)
    except Exception:  # noqa: BLE001  # a summary must never mask the failure
        return _bounded(error_type)


def record_failure_cause(
    runtime: ObservabilityRuntime | None,
    error: BaseException,
    summary: str,
) -> None:
    """Attach the redacted cause, with the original stack, to the operation."""
    if runtime is None:
        return
    try:
        cause = Stage12FailureCause(summary).with_traceback(error.__traceback__)
        runtime.record_exception(cause, handled=True)
    except Exception:  # noqa: BLE001  # telemetry must not change the outcome
        logger.warning("stage12_failure_cause_not_recorded")


def _simulation_role(attributes: Mapping[str, object] | None) -> str | None:
    role = attributes.get("simulation_role") if attributes is not None else None
    return str(role) if role is not None else None


def _location(progress: StageProgress | None) -> str:
    if progress is None:
        return ""
    location = ""
    if progress.stage_name is not None:
        position = "after" if progress.stage_finished else "in"
        location = f" {position} {progress.stage_name}"
    if progress.stage_role is not None:
        location = f"{location} ({progress.stage_role})"
    return location


def _failure_message(error: BaseException) -> str:
    if isinstance(error, ValidationError):
        return _validation_message(error)
    if isinstance(error, StatementError) and error.orig is not None:
        # SQLAlchemy wraps the driver error with the statement and its
        # parameters, so only the driver error is reported. Its detail can
        # still echo row values, which redaction removes.
        return f"{type(error.orig).__name__}: {error.orig}"
    return str(error)


def _validation_message(error: ValidationError) -> str:
    details = []
    for detail in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        description = f"{detail['msg']} [{detail['type']}]"
        location = ".".join(str(part) for part in detail["loc"])
        details.append(f"{location}: {description}" if location else description)
    count = error.error_count()
    noun = "error" if count == 1 else "errors"
    return f"{count} validation {noun} for {error.title}: {'; '.join(details)}"


def _without_url_query(url_match: re.Match[str]) -> str:
    if url_match["query"] is None:
        return url_match[0]
    return f"{url_match['address']}?{REDACTED}"


def _before_last_word(text: str) -> str:
    words = text.rsplit(maxsplit=1)
    return words[0] if len(words) == 2 else ""


def _secret_environment_values() -> tuple[str, ...]:
    values: list[str] = []
    for name in SECRET_ENVIRONMENT_VARIABLES:
        value = os.environ.get(name, "")
        values.append(value)
        values.append(_url_password(value) or "")
    # Very short values would redact ordinary words; URL credentials are
    # removed by pattern regardless of length.
    secrets = {value for value in values if len(value) >= MIN_SECRET_VALUE_LENGTH}
    # Replace longer values first so a password inside a URL is not split.
    return tuple(sorted(secrets, key=len, reverse=True))


def _url_password(value: str) -> str | None:
    try:
        return urlsplit(value).password
    except ValueError:
        return None


def _bounded(summary: str) -> str:
    if len(summary) <= MAX_FAILURE_SUMMARY_LENGTH:
        return summary
    kept = MAX_FAILURE_SUMMARY_LENGTH - len(TRUNCATION_MARKER)
    return f"{summary[:kept]}{TRUNCATION_MARKER}"

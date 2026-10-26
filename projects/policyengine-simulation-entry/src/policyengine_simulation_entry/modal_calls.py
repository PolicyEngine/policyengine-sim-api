"""Start Modal calls and ask about them without waiting, for simulation runs.

The Simulation Entrypoint starts each run's Modal call with ``spawn`` and later
asks Modal about it with ``get(timeout=0)``, which returns at once. Modal stops
a pre-empted worker with KeyboardInterrupt, records no result for it and
re-runs the same input under the same call ID, so the call reads as still
running and finishes later: pre-emption is never a failure.

These adapters belong to the main path. The Stage 12 comparison path keeps its
own Modal code; the two share nothing.
"""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from typing import Any, Protocol

import modal
from policyengine_simulation_persistence import RunError, RunErrorCode


@dataclass(frozen=True)
class ModalTarget:
    """A deployed Modal function: its application and function names."""

    application: str
    function: str


@dataclass(frozen=True)
class ModalRequest:
    """One call to start: the target, its positional arguments and telemetry.

    ``arguments`` hold calculation inputs and functional metadata only.
    ``observability_context`` travels as Modal's separate keyword argument.
    """

    target: ModalTarget
    arguments: tuple[object, ...]
    observability_context: dict[str, Any] | None = None


@dataclass(frozen=True)
class CallRunning:
    """The call has produced no output yet, or Modal is re-running it."""


@dataclass(frozen=True)
class CallSucceeded:
    """The call returned ``output``."""

    output: object


@dataclass(frozen=True)
class CallFailed:
    """The call failed; the error carries no calculation data."""

    error: RunError


type CallOutcome = CallRunning | CallSucceeded | CallFailed


class CallInspectionError(RuntimeError):
    """Modal could not be asked about a call; ask again later."""


class CallDispatcher(Protocol):
    """Start a Modal call without waiting for it, or cancel one."""

    async def spawn(self, request: ModalRequest) -> str:
        """Return the call ID once Modal acknowledges the call."""
        ...

    async def cancel(self, modal_call_id: str) -> None:
        """Ask Modal to stop a call whose runs could not record it."""
        ...


class CallInspector(Protocol):
    """Ask Modal about a call without waiting for it."""

    async def inspect(self, modal_call_id: str) -> CallOutcome:
        """Return the call's outcome; raise CallInspectionError if unknown."""
        ...


class ModalCallDispatcher:
    """Spawn deployed Modal functions of one Modal environment."""

    def __init__(self, environment: str) -> None:
        """Look functions up in ``environment``."""
        self._environment = environment

    async def spawn(self, request: ModalRequest) -> str:
        """Spawn the request's function and return Modal's call ID.

        Raise RuntimeError when Modal acknowledges the call without an ID.
        """
        function = modal.Function.from_name(
            request.target.application,
            request.target.function,
            environment_name=self._environment,
        )
        call = await function.spawn.aio(
            *request.arguments,
            observability_context=request.observability_context,
        )
        modal_call_id = getattr(call, "object_id", None)
        if not isinstance(modal_call_id, str) or not modal_call_id:
            msg = "Modal acknowledged the call without a call ID"
            raise RuntimeError(msg)
        return modal_call_id

    async def cancel(self, modal_call_id: str) -> None:
        """Ask Modal to stop the call; its output will never be collected."""
        await modal.FunctionCall.from_id(modal_call_id).cancel.aio()


# Where Modal raises a finished call's own outcome in this process: the
# worker's exception (whatever its type, followed in the traceback by the
# worker's own frames), the call's timeout, a failure Modal records for the
# call, or an output that cannot be decoded. Anything raised elsewhere came
# from this process's own request to Modal.
_CALL_RESULT_MODULE = "modal._utils.function_utils"
_CALL_RESULT_FUNCTION = "_process_result"
# Before raising anything, that function fetches an output Modal stored as a
# blob (any spawned call's output over 8 KiB) with this process's own
# requests; an error raised in them is not the call's outcome.
_OUTPUT_DOWNLOAD_MODULE = "modal._utils.blob_utils"
# Signals to this process itself, which must never be taken for an outcome.
OWN_PROCESS_SIGNALS: tuple[type[BaseException], ...] = (
    asyncio.CancelledError,
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
)


def raised_from_call_result(error: BaseException) -> bool:
    """Return whether Modal raised ``error`` as a finished call's outcome.

    That is so when Modal's result processing raised it, or re-raised the
    worker's exception, but not when the error came from the output download
    that processing makes first.
    """
    frames = [frame for frame, _ in traceback.walk_tb(error.__traceback__)]
    for position, frame in enumerate(frames):
        if (
            frame.f_globals.get("__name__") == _CALL_RESULT_MODULE
            and frame.f_code.co_name == _CALL_RESULT_FUNCTION
        ):
            # The frame it called, if any: a download, or the worker's own.
            callee = frames[position + 1] if position + 1 < len(frames) else None
            return (
                callee is None
                or callee.f_globals.get("__name__") != _OUTPUT_DOWNLOAD_MODULE
            )
    return False


def _failed(code: RunErrorCode, message: str) -> CallFailed:
    return CallFailed(RunError(code, message))


class ModalCallInspector:
    """Read a Modal call's output if it has one, without waiting."""

    async def inspect(self, modal_call_id: str) -> CallOutcome:
        """Return the call's outcome, or raise CallInspectionError.

        A worker's exception is re-raised here by Modal and can be of any
        type, including one Modal's client also raises (a built-in
        TimeoutError, a Modal or grpclib error) or a BaseException such as
        SystemExit. So the outcome is decided by where the exception was
        raised: from the finished call's result, it is the call's outcome;
        otherwise it came from this process's own request to Modal, and a
        signal to this process propagates.
        """
        try:
            call = modal.FunctionCall.from_id(modal_call_id)
            output = await call.get.aio(timeout=0)
        except BaseException as error:
            if raised_from_call_result(error):
                return _call_failure(error)
            if isinstance(error, OWN_PROCESS_SIGNALS) or not isinstance(
                error, Exception
            ):
                raise
            return _request_outcome(error)
        return CallSucceeded(output)


def _call_failure(error: BaseException) -> CallFailed:
    """Turn a finished call's own outcome into a run failure."""
    if isinstance(error, modal.exception.FunctionTimeoutError):
        return _failed(RunErrorCode.WORKER_TIMEOUT, "The Modal call timed out.")
    # Record only the type: the message may contain calculation inputs.
    return _failed(
        RunErrorCode.WORKER_ERROR, f"The Modal call raised {type(error).__name__}."
    )


def _request_outcome(error: Exception) -> CallOutcome:
    """Read an error from this process's own request to Modal.

    Raise CallInspectionError for a problem reaching Modal, so the call is
    asked about again later.
    """
    # Modal raises the built-in TimeoutError while the call has no output,
    # including while it re-runs a pre-empted call.
    if type(error) is TimeoutError:
        return CallRunning()
    if isinstance(error, modal.exception.OutputExpiredError):
        return _failed(
            RunErrorCode.RESULT_EXPIRED, "Modal no longer holds the call's output."
        )
    if isinstance(error, modal.exception.NotFoundError):
        return _failed(RunErrorCode.CALL_NOT_FOUND, "Modal does not know the call.")
    raise CallInspectionError(type(error).__name__) from error

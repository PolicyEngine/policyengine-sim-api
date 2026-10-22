"""Start Modal calls and ask about them without waiting, for simulation runs.

The Simulation Entrypoint starts each run's Modal call with ``spawn`` and later
asks Modal about it with ``get(timeout=0)``, which returns at once. A call
Modal pre-empted is re-run on the same input under the same call ID, so it
reads as still running and finishes later: pre-emption is never a failure.

These adapters belong to the main path. The Stage 12 comparison path keeps its
own Modal code; the two share nothing.
"""

from __future__ import annotations

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


# Problems reaching Modal: the call's state is unknown, so ask again later.
TRANSIENT_MODAL_ERRORS: tuple[type[Exception], ...] = (
    modal.exception.ConnectionError,
    modal.exception.AuthError,
    modal.exception.ServiceError,
    modal.exception.ResourceExhaustedError,
    modal.exception.InternalError,
)


def _failed(code: RunErrorCode, message: str) -> CallFailed:
    return CallFailed(RunError(code, message))


class ModalCallInspector:
    """Read a Modal call's output if it has one, without waiting."""

    async def inspect(self, modal_call_id: str) -> CallOutcome:
        """Return the call's outcome, or raise CallInspectionError."""
        try:
            call = modal.FunctionCall.from_id(modal_call_id)
            output = await call.get.aio(timeout=0)
        # Modal raises the built-in TimeoutError while the call has no output.
        except TimeoutError:
            return CallRunning()
        except modal.exception.OutputExpiredError:
            return _failed(
                RunErrorCode.RESULT_EXPIRED, "Modal no longer holds the call's output."
            )
        except modal.exception.FunctionTimeoutError:
            return _failed(RunErrorCode.WORKER_TIMEOUT, "The Modal call timed out.")
        except modal.exception.NotFoundError:
            return _failed(RunErrorCode.CALL_NOT_FOUND, "Modal does not know the call.")
        except TRANSIENT_MODAL_ERRORS as error:
            raise CallInspectionError(type(error).__name__) from error
        # The call re-raises whatever the worker raised. Record only its type:
        # the message may contain calculation inputs.
        except Exception as error:  # noqa: BLE001  # every worker failure fails the run
            return _failed(
                RunErrorCode.WORKER_ERROR,
                f"The Modal call raised {type(error).__name__}.",
            )
        return CallSucceeded(output)

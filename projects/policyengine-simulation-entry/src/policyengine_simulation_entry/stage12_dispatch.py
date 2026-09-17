"""Bounded dispatch for non-authoritative Stage 12 calculation copies."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from policyengine_observability import record_event

logger = logging.getLogger(__name__)


class Stage12CopyBackend(Protocol):
    async def dispatch_after_production(
        self,
        *,
        request_payload: dict[str, Any],
        production_response: bytes,
        request_id: str,
    ) -> None: ...


@dataclass(frozen=True)
class Stage12CopyRequest:
    """The immutable inputs needed to dispatch one Stage 12 copy."""

    request_payload: dict[str, Any]
    production_response: bytes
    request_id: str


class Stage12CopyDispatcher:
    """Admit Stage 12 copies without creating one task per production request.

    A fixed worker set and bounded queue cap the memory and task resources used
    by the non-authoritative calculation path. Capacity and timeout failures are
    observable, but are deliberately not returned to the v1 caller.
    """

    def __init__(
        self,
        backend: Stage12CopyBackend,
        *,
        max_in_flight: int,
        queue_capacity: int,
        timeout_seconds: float,
    ) -> None:
        self._backend = backend
        self._max_in_flight = max_in_flight
        self._timeout_seconds = timeout_seconds
        self._queue: asyncio.Queue[Stage12CopyRequest] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._workers: list[asyncio.Task[None]] = []
        self._accepting = False

    async def start(self) -> None:
        """Start the fixed worker set after application startup succeeds."""

        if self._workers:
            return
        self._accepting = True
        self._workers = [
            asyncio.create_task(
                self._run_worker(),
                name=f"stage12-copy-dispatch-{worker_number}",
            )
            for worker_number in range(self._max_in_flight)
        ]

    async def close(self) -> None:
        """Stop admission and cancel queued or active non-authoritative work."""

        self._accepting = False
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    def submit(
        self,
        *,
        request_payload: dict[str, Any],
        production_response: bytes,
        request_id: str,
    ) -> bool:
        """Admit a copy immediately, or report bounded-capacity exhaustion."""

        if not self._accepting:
            self._record_rejection(
                request_id=request_id,
                reason="dispatcher_not_running",
            )
            return False
        try:
            self._queue.put_nowait(
                Stage12CopyRequest(
                    request_payload=request_payload,
                    production_response=production_response,
                    request_id=request_id,
                )
            )
        except asyncio.QueueFull:
            self._record_rejection(
                request_id=request_id,
                reason="capacity_exhausted",
            )
            return False
        return True

    async def _run_worker(self) -> None:
        while True:
            request = await self._queue.get()
            dispatch = asyncio.create_task(
                self._backend.dispatch_after_production(
                    request_payload=request.request_payload,
                    production_response=request.production_response,
                    request_id=request.request_id,
                )
            )
            try:
                await asyncio.wait_for(
                    asyncio.shield(dispatch),
                    timeout=self._timeout_seconds,
                )
            except TimeoutError:
                logger.error(
                    "stage12_copy_dispatch_timed_out",
                    extra={
                        "request_id": request.request_id,
                        "timeout_seconds": self._timeout_seconds,
                    },
                )
                self._record_event_safely(
                    "stage12_copy_dispatch_timed_out",
                    request_id=request.request_id,
                    calculation_flow="economy",
                )
                # Stage12EvaluationBackend delegates synchronous database and
                # Modal calls to a thread. Python cannot safely terminate that
                # thread, so retain this fixed worker slot until it returns.
                # This prevents timed-out calls from accumulating elsewhere.
                try:
                    await dispatch
                except Exception as error:
                    logger.error(
                        "stage12_copy_dispatch_failed_after_timeout",
                        extra={
                            "request_id": request.request_id,
                            "error_type": type(error).__name__,
                        },
                    )
            except asyncio.CancelledError:
                dispatch.cancel()
                await asyncio.gather(dispatch, return_exceptions=True)
                raise
            except Exception as error:
                logger.error(
                    "stage12_copy_dispatch_failed",
                    extra={
                        "request_id": request.request_id,
                        "error_type": type(error).__name__,
                    },
                )
                self._record_event_safely(
                    "stage12_copy_dispatch_failed",
                    request_id=request.request_id,
                    calculation_flow="economy",
                )
            finally:
                self._queue.task_done()

    def _record_rejection(self, *, request_id: str, reason: str) -> None:
        logger.warning(
            "stage12_copy_dispatch_not_admitted",
            extra={
                "request_id": request_id,
                "reason": reason,
                "queue_capacity": self._queue.maxsize,
                "max_in_flight": self._max_in_flight,
            },
        )
        self._record_event_safely(
            "stage12_copy_dispatch_not_admitted",
            request_id=request_id,
            calculation_flow="economy",
            reason=reason,
        )

    @staticmethod
    def _record_event_safely(event_name: str, **attributes: object) -> None:
        try:
            record_event(event_name, **attributes)
        except Exception:
            # Observability is also non-authoritative and cannot affect v1.
            logger.exception(
                "stage12_copy_dispatch_observability_failed",
                extra={"event_name": event_name},
            )

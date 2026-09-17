from __future__ import annotations

import asyncio

import pytest

from policyengine_simulation_entry import stage12_dispatch as dispatch_module
from policyengine_simulation_entry.stage12_dispatch import Stage12CopyDispatcher


class BlockingBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.first_started = asyncio.Event()
        self.release = asyncio.Event()

    async def dispatch_after_production(self, *, request_id: str, **_) -> None:
        self.calls.append(request_id)
        self.first_started.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_dispatcher_bounds_active_and_waiting_copies(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(
        dispatch_module,
        "record_event",
        lambda event_name, **attributes: events.append((event_name, attributes)),
    )
    backend = BlockingBackend()
    dispatcher = Stage12CopyDispatcher(
        backend,
        max_in_flight=1,
        queue_capacity=1,
        timeout_seconds=10,
    )
    await dispatcher.start()

    try:
        assert dispatcher.submit(
            request_payload={},
            production_response=b"{}",
            request_id="active",
        )
        await asyncio.wait_for(backend.first_started.wait(), timeout=1)
        assert dispatcher.submit(
            request_payload={},
            production_response=b"{}",
            request_id="waiting",
        )
        assert not dispatcher.submit(
            request_payload={},
            production_response=b"{}",
            request_id="rejected",
        )
    finally:
        backend.release.set()
        await dispatcher.close()

    assert events == [
        (
            "stage12_copy_dispatch_not_admitted",
            {
                "request_id": "rejected",
                "calculation_flow": "economy",
                "reason": "capacity_exhausted",
            },
        )
    ]


@pytest.mark.asyncio
async def test_dispatcher_times_out_copy_without_raising_to_caller(monkeypatch) -> None:
    events = []
    cancelled = asyncio.Event()
    event_recorded = asyncio.Event()

    class SlowBackend:
        async def dispatch_after_production(self, **_) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    def record_event(event_name, **attributes):
        events.append((event_name, attributes))
        event_recorded.set()

    monkeypatch.setattr(dispatch_module, "record_event", record_event)
    dispatcher = Stage12CopyDispatcher(
        SlowBackend(),
        max_in_flight=1,
        queue_capacity=1,
        timeout_seconds=0.01,
    )
    await dispatcher.start()

    try:
        assert dispatcher.submit(
            request_payload={},
            production_response=b"{}",
            request_id="slow",
        )
        await asyncio.wait_for(event_recorded.wait(), timeout=1)
        assert not cancelled.is_set()
    finally:
        await dispatcher.close()

    assert cancelled.is_set()
    assert events == [
        (
            "stage12_copy_dispatch_timed_out",
            {
                "request_id": "slow",
                "calculation_flow": "economy",
            },
        )
    ]

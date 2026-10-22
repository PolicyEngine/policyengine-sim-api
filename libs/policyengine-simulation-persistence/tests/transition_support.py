"""A clock the tests move by hand, and stores that share it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from policyengine_simulation_persistence import (
    RunStore,
    RunTransitions,
    create_simulation_engine,
)


@dataclass
class ManualClock:
    """Return a fixed time until the test advances it."""

    now: datetime = field(
        default_factory=lambda: datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    )

    def __call__(self) -> datetime:
        """Return the current fake time."""
        return self.now

    def advance(self, interval: timedelta) -> None:
        """Move the fake time forward by ``interval``."""
        self.now += interval


@dataclass(frozen=True)
class ClockedStores:
    """A run store and transitions on one runtime engine and one clock."""

    runs: RunStore
    transitions: RunTransitions
    clock: ManualClock


def clocked_stores(database, clock: ManualClock | None = None) -> ClockedStores:
    """Return stores that connect as the runtime role and share ``clock``."""
    clock = clock or ManualClock()
    engine = create_simulation_engine(database.runtime_url)
    return ClockedStores(
        runs=RunStore(engine, clock=clock),
        transitions=RunTransitions(engine, clock=clock),
        clock=clock,
    )

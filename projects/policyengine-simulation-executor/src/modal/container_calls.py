"""How the container serving a Modal worker call started, and the call's number.

Both Modal apps' workers set two attributes on each call's operation:
``container_start`` (a ``ContainerStart`` value) and
``container_call_number``. Each app keeps one ``ContainerCalls`` at module
level: a Modal container serves one worker class or function, so that state
is the container's. ``docs/engineering/skills/observability.md`` describes
both attributes.
"""

from __future__ import annotations

import contextlib
import itertools
import os
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from policyengine_observability import ObservabilityRuntime


class ContainerStart(StrEnum):
    """How the container serving a worker call started: its ``container_start``."""

    RESTORED_FROM_SNAPSHOT = "restored_from_snapshot"
    TOOK_SNAPSHOT = "took_snapshot"
    NO_SNAPSHOT = "no_snapshot"


class ContainerCalls:
    """Says how this container started, and numbers the worker calls it serves.

    The workers' snapshot hook records whether Modal snapshots the container
    it runs in, and that container's ``MODAL_TASK_ID``, before Modal takes the
    snapshot. A container restored from the snapshot starts with that record
    in memory, but Modal 1.6.4 sets its ``MODAL_TASK_ID`` to its own task ID
    (``memory_restore`` in ``modal/_runtime/task_lifecycle_manager.py``), so
    the two IDs differ. The recorded ID is only compared, never reported: it
    names the snapshotting container, not the one serving the call.

    The call counter is in the snapshot too, but no call runs before Modal
    takes it, so every container's first call is number 1.
    """

    def __init__(self) -> None:
        """Start as a container no snapshot hook has run in, with no calls yet."""
        self._snapshot_taken_here = False
        self._snapshot_task_id: str | None = None
        self._call_numbers = itertools.count(1)

    def record_snapshot_hook(self) -> None:
        """Record, from the snapshot hook, whether Modal snapshots this container.

        Modal 1.6.4 runs the hook in every container that isn't restored, and
        takes a snapshot only when the Modal process that starts the container
        has set ``MODAL_ENABLE_SNAP_RESTORE`` to ``1`` (``maybe_snapshot`` in
        ``modal/_runtime/user_code_imports.py``). The flag is set last, so a
        failure partway leaves the container reporting ``no_snapshot``.
        """
        self._snapshot_task_id = os.environ.get("MODAL_TASK_ID")
        self._snapshot_taken_here = os.environ.get("MODAL_ENABLE_SNAP_RESTORE") == "1"

    def container_start(self) -> ContainerStart:
        """Compare this container's task ID with the snapshotting container's."""
        if not self._snapshot_taken_here:
            return ContainerStart.NO_SNAPSHOT
        if os.environ.get("MODAL_TASK_ID") == self._snapshot_task_id:
            return ContainerStart.TOOK_SNAPSHOT
        return ContainerStart.RESTORED_FROM_SNAPSHOT

    def next_call_attributes(self) -> dict[str, str | int]:
        """Return the next call's two container attributes, numbering the call.

        The call takes its number first, so it counts even if reading the
        container start fails.
        """
        call_number = next(self._call_numbers)
        return {
            "container_start": self.container_start().value,
            "container_call_number": call_number,
        }

    def set_call_attributes(self, runtime: ObservabilityRuntime) -> None:
        """Set the next call's two attributes on the operation open in ``runtime``.

        Call it once per worker call, as the call's operation opens, so a call
        that fails or is preempted carries them too. Telemetry is non-fatal:
        a failure to read or set them raises nothing, and the call still
        counts.
        """
        with contextlib.suppress(Exception):
            runtime.set_context(**self.next_call_attributes())

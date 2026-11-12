"""What the v2 calculation routes share: their failure logs and trace context.

The household and economy routes answer an unexpected failure with a JSON
500 and log it as a fixed event with the error's stack frames
(``log_unexpected_failure``), and pass each submission's trace context and
observability ID to the Modal call they start (``observability_context``).
"""

from __future__ import annotations

import contextlib
import traceback
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging

    from policyengine_simulation_entry.entry_runtime import EntryRuntime

# Separates the frames in the 500 log's ``stack_frames`` attribute.
STACK_FRAME_SEPARATOR = "; "
# The service's own packages, whose frames the 500 log keeps before others.
SERVICE_PACKAGES = frozenset(
    {"policyengine_simulation_entry", "policyengine_simulation_persistence"}
)
# The directories a package's files sit under: an installed package's, then
# a checkout's. A frame's path is shortened to its part after one of them.
_PACKAGE_PARENT_DIRECTORIES = ("/site-packages/", "/src/")


def package_path(file_path: str) -> str:
    """Return ``file_path`` from its package's directory on.

    That is its part after the last ``site-packages/``, where the image
    installs the service and its libraries, or else after the last ``src/``,
    where a checkout keeps the service's packages. Any other path is
    returned whole.
    """
    for parent_directory in _PACKAGE_PARENT_DIRECTORIES:
        _, found, path_after_parent = file_path.rpartition(parent_directory)
        if found:
            return path_after_parent
    return file_path


def stack_frames(error: Exception, max_characters: int) -> tuple[str, int]:
    """Return the error's frames that fit ``max_characters``, and how many don't.

    Each frame is ``path:line in function``, with its path from its
    package's directory on (``package_path``). The kept frames are written
    innermost first, from where the error was raised out to the route's
    handler. When they don't all fit, frames are taken in this order until
    the next one would pass ``max_characters``: the frame that raised the
    error, then the service's own frames (``SERVICE_PACKAGES``) from the
    innermost out, then the others from the innermost out. So other
    libraries' outer frames are left out first, and left-out frames may sit
    between two kept ones. Staying within ``max_characters`` keeps the
    string whole: the observability runtime keeps only the start of a longer
    string attribute.
    """
    frames: list[str] = []
    from_the_service: list[bool] = []
    for frame in reversed(traceback.extract_tb(error.__traceback__)):
        path_in_package = package_path(frame.filename)
        frames.append(f"{path_in_package}:{frame.lineno} in {frame.name}")
        from_the_service.append(path_in_package.split("/", 1)[0] in SERVICE_PACKAGES)
    positions_by_priority = sorted(
        range(len(frames)),
        key=lambda position: (position > 0, not from_the_service[position], position),
    )
    kept_positions: list[int] = []
    length = 0
    for position in positions_by_priority:
        added = len(frames[position]) + (
            len(STACK_FRAME_SEPARATOR) if kept_positions else 0
        )
        if length + added > max_characters:
            break
        kept_positions.append(position)
        length += added
    kept = [frames[position] for position in sorted(kept_positions)]
    return STACK_FRAME_SEPARATOR.join(kept), len(frames) - len(kept)


def log_unexpected_failure(
    logger: logging.Logger,
    event: str,
    *,
    route: str,
    error: Exception,
    max_characters: int,
) -> None:
    """Log an unexpected failure of ``route`` as the fixed ``event``.

    The record carries the route, the error's type, its stack frames (path,
    line and function; see ``stack_frames``) as one string of at most
    ``max_characters``, the observability runtime's string limit, and the
    number of frames left out. It never carries the error's message or any
    value, which could repeat request or stored data. The fields go in
    ``policyengine_attributes`` too, the only extra fields the observability
    runtime's log handler keeps, and each is a string or an integer: the
    runtime keeps only strings, numbers and booleans.
    """
    frames, omitted_frame_count = stack_frames(error, max_characters)
    fields = {
        "route": route,
        "error_type": type(error).__name__,
        "stack_frames": frames,
        "stack_frames_omitted": omitted_frame_count,
    }
    logger.error(event, extra={**fields, "policyengine_attributes": fields})


def observability_context(entry: EntryRuntime, observability_id: str) -> dict[str, Any]:
    """Capture the trace context for the Modal call a submission starts, with the ID.

    Telemetry is non-fatal: without a captured context, only the ID is sent.
    """
    captured: object = None
    with contextlib.suppress(Exception):
        captured = entry.runtime.capture_context()
    context = dict(captured) if isinstance(captured, dict) else {}
    context["observability_id"] = observability_id
    return context

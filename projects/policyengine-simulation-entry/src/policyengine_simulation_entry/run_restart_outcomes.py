"""How the restart check ended for each row and budget window it took up."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from policyengine_simulation_persistence import IdleBudgetWindow, UnstartedRun

logger = logging.getLogger(__name__)

# What one restart takes up: a row whose call never started, or a budget
# window that stopped with years still waiting.
type RestartTarget = UnstartedRun | IdleBudgetWindow

# The code and summary a row the check could not start again records.
NOT_STARTED_MESSAGE: Final = (
    "run_not_started: The run stayed pending with no Modal call recorded for it"
)


class RestartOutcome(StrEnum):
    """How the check ended for one listed row or budget window.

    ``restarted``: its new call started, and the row records that call; a
    window's advance started at least one of its years.
    ``call_not_recorded``: its new call started, but the check couldn't
    record or read the call's ID; the call claims the row itself. So too a
    window whose advance failed after Modal accepted a year's call.
    ``started_meanwhile``: the row records another call, which claimed it
    after the check marked it (or the check's own call, accepted after the
    check stopped waiting for it); the other call then can't claim it.
    ``left``: the row was gone, changed before the check started a call, or
    failed meanwhile; also a marked row whose failure couldn't be written,
    which the sweep fails later, and a row whose call couldn't be read
    after Modal didn't confirm the check's call. A window is left when it is
    gone, has no year waiting untouched, finished or was failed for an
    earlier failed year before the check started anything, or its year
    changed before the mark; and when its marked year's failure couldn't be
    written, for the sweep to fail as stalled.
    ``deferred``: the row or window is left unmarked for the next check,
    because its rows or the manifest couldn't be read, its restart raised
    an unexpected error before any mark (logged with its traceback), the
    failure of a row that can't be rebuilt couldn't be written, the mark
    couldn't be written, a window's lock or reads failed before any start,
    or the check's time ran out.
    ``failed``: it couldn't be started again and was failed with
    ``run_not_started``. A window fails when Modal refuses one of its years
    (``modal_call_refused``), or its first year's call can't be rebuilt
    (that year records ``run_not_started`` and why); the window then
    records which year failed.
    """

    RESTARTED = "restarted"
    CALL_NOT_RECORDED = "call_not_recorded"
    STARTED_MEANWHILE = "started_meanwhile"
    LEFT = "left"
    DEFERRED = "deferred"
    FAILED = "failed"


def log_unexpected_error(target: RestartTarget) -> None:
    """Log the error being handled, with its traceback, naming the row or window."""
    fields = {"run_kind": target.kind.value, "run_id": str(target.run_id)}
    # The observability runtime's log handler writes only
    # ``policyengine_attributes``, as route_support's own log does.
    logger.exception(
        "v2_run_restart_unexpected_error",
        extra={**fields, "policyengine_attributes": fields},
    )

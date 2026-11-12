"""How the restart check ended for each row it took up."""

from __future__ import annotations

from enum import StrEnum


class RestartOutcome(StrEnum):
    """How the check ended for one listed row.

    ``restarted``: its new call started, and the row records that call.
    ``call_not_recorded``: its new call started, but the check couldn't
    record or read the call's ID; the call claims the row itself.
    ``started_meanwhile``: the row records another call, which claimed it
    after the check marked it (or the check's own call, accepted after the
    check stopped waiting for it); the other call then can't claim it.
    ``left``: the row was gone, changed before the check started a call, or
    failed meanwhile; also a marked row whose failure couldn't be written,
    which the sweep fails later, and a row whose call couldn't be read
    after Modal didn't confirm the check's call. ``deferred``: the row is
    left unmarked for the next check, because its rows or the manifest
    couldn't be read, rebuilding its call raised an unexpected error (logged
    with its traceback), the failure of a row that can't be rebuilt couldn't
    be written, the mark couldn't be written, or the check's time ran out.
    ``failed``: it couldn't be started again and was failed with
    ``run_not_started``.
    """

    RESTARTED = "restarted"
    CALL_NOT_RECORDED = "call_not_recorded"
    STARTED_MEANWHILE = "started_meanwhile"
    LEFT = "left"
    DEFERRED = "deferred"
    FAILED = "failed"

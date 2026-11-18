"""Validate the shared API v2 runtime credential and Stage 12 schema."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from policyengine_simulation_persistence import dispose_session_factory
from policyengine_stage12_persistence import (
    Stage12RuntimeAccessError,
    create_stage12_check_session_factory,
    validate_runtime_database,
)

# The check's own attempts and waits fit well within this. A connection that
# goes silent raises no error, so nothing else would end a read blocked on it.
CHECK_TIME_LIMIT_SECONDS = 300


def _end_the_stalled_check() -> None:
    """Fail the deployment at once: cleaning up could block on the same read."""
    print(
        "Stage 12 runtime database verification did not finish within "
        f"{CHECK_TIME_LIMIT_SECONDS} seconds",
        file=sys.stderr,
        flush=True,
    )
    os._exit(1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--environment", required=True)
    return parser


def main() -> None:
    """Check the runtime database credential from the command line.

    Exit with the error message if the role, environment or schema does not
    match what the Stage 12 runtime expects.
    """
    args = _parser().parse_args()
    database_url = args.database_url_file.read_text(encoding="utf-8").strip()
    time_limit = threading.Timer(CHECK_TIME_LIMIT_SECONDS, _end_the_stalled_check)
    time_limit.daemon = True
    time_limit.start()
    try:
        session_factory = create_stage12_check_session_factory(database_url)
        try:
            validate_runtime_database(
                session_factory,
                expected_role=args.expected_role,
                environment=args.environment,
            )
        finally:
            dispose_session_factory(session_factory)
    except (Stage12RuntimeAccessError, ValueError) as error:
        raise SystemExit(str(error)) from None
    finally:
        time_limit.cancel()
    print(
        "Shared API v2 runtime database access verified for Stage 12: "
        f"environment={args.environment}, role={args.expected_role}"
    )


if __name__ == "__main__":
    main()

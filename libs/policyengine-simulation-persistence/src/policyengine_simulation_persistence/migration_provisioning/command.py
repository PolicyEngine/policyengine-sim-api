"""The command an operator runs to provision the one-time migration's reports.

``python -m policyengine_simulation_persistence.migration_provisioning
--definitions FILE (--output FILE | --dry-run)``

It connects with the URL in ``SIMULATION_DATABASE_URL``, which must log in as
the runtime role, to a database at the revision this code expects. Before it
connects it checks the definitions file and, for a run, that the output file
does not exist yet. A run writes the output file at the end; a dry run writes
nothing. Both print a JSON summary on standard output.

Exit status: 0 when every report was provisioned (or, in a dry run, would be);
1 when some reports failed (or would fail), which the summary and a run's
output file list; 2 when the command refused to start, and nothing was
written; 3 when the database stopped the command partway, or a run's output
file could not be written. Rows a run created before a stop are kept, and
running the command again reuses them; a dry run writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_persistence.engine import (
    create_simulation_engine,
    read_schema_revision,
)
from policyengine_simulation_persistence.migration_provisioning.definitions import (
    ReportDefinitionsFile,
)
from policyengine_simulation_persistence.migration_provisioning.mapping import (
    ReportMappingFile,
)
from policyengine_simulation_persistence.migration_provisioning.provisioning import (
    database_error_summary,
    provision_reports,
)
from policyengine_simulation_persistence.reports import ReportStore
from policyengine_simulation_persistence.schema import HEAD_REVISION, RUNTIME_ROLE
from policyengine_simulation_persistence.simulations import SimulationStore

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy import Engine

    from policyengine_simulation_persistence.migration_provisioning import (
        ProvisioningResult,
    )

DATABASE_URL_VARIABLE = "SIMULATION_DATABASE_URL"
EXIT_PROVISIONED = 0
EXIT_REPORTS_FAILED = 1
EXIT_REFUSED = 2
EXIT_STOPPED = 3
# A definitions file with more errors lists only the first ones.
LISTED_ERROR_LIMIT = 20


class CommandRefusedError(Exception):
    """The command cannot start; the message says why."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m policyengine_simulation_persistence.migration_provisioning",
        description=(
            "Create the simulations and reports of exported v1 report "
            "definitions, with no run, and write each v1 report's v2 report ID. "
            f"Connects with the runtime role's URL in {DATABASE_URL_VARIABLE}."
        ),
    )
    parser.add_argument(
        "--definitions",
        type=Path,
        required=True,
        help="the Public API's export of v1 report definitions (JSON)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="where a run writes the report mapping; it must not exist yet",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read the database and report what a run would do, writing nothing",
    )
    return parser


@dataclass(frozen=True)
class _Arguments:
    definitions: Path
    # Where a run writes the report mapping; None for a dry run.
    output: Path | None

    @property
    def dry_run(self) -> bool:
        return self.output is None


def _parse_arguments(argv: Sequence[str] | None) -> _Arguments:
    parser = _parser()
    parsed = parser.parse_args(argv)
    if parsed.dry_run and parsed.output is not None:
        parser.error("--dry-run writes no file, so it takes no --output")
    if not parsed.dry_run and parsed.output is None:
        parser.error("a run needs --output, or use --dry-run")
    return _Arguments(definitions=parsed.definitions, output=parsed.output)


def _check_output_path(output: Path) -> None:
    if output.exists():
        msg = f"{output} already exists; choose a new output file"
        raise CommandRefusedError(msg)
    if not output.parent.is_dir():
        msg = f"{output.parent} is not a directory"
        raise CommandRefusedError(msg)
    if not os.access(output.parent, os.W_OK | os.X_OK):
        msg = f"cannot write to {output.parent}"
        raise CommandRefusedError(msg)


def _validation_lines(error: ValidationError) -> list[str]:
    details = error.errors(include_url=False, include_input=False)
    lines = [
        "  "
        + (".".join(str(part) for part in detail["loc"]) or "file")
        + ": "
        + detail["msg"]
        for detail in details[:LISTED_ERROR_LIMIT]
    ]
    if len(details) > LISTED_ERROR_LIMIT:
        lines.append(f"  and {len(details) - LISTED_ERROR_LIMIT} more")
    return lines


def _read_definitions(path: Path) -> ReportDefinitionsFile:
    try:
        content = path.read_bytes()
    except OSError as error:
        msg = f"cannot read {path}: {error.strerror}"
        raise CommandRefusedError(msg) from error
    try:
        return ReportDefinitionsFile.model_validate_json(content)
    except ValidationError as error:
        msg = "\n".join(
            [
                f"{path} is not a valid report definitions file:",
                *_validation_lines(error),
            ]
        )
        raise CommandRefusedError(msg) from error


def _runtime_engine(environ: Mapping[str, str]) -> Engine:
    database_url = environ.get(DATABASE_URL_VARIABLE, "")
    if not database_url:
        msg = f"{DATABASE_URL_VARIABLE} is not set"
        raise CommandRefusedError(msg)
    try:
        return create_simulation_engine(database_url)
    except ValueError as error:
        # The message never repeats the URL, which holds a password.
        raise CommandRefusedError(str(error)) from None


def _check_database(engine: Engine) -> None:
    try:
        with engine.connect() as connection:
            role = connection.execute(select(func.current_user())).scalar_one()
            revision = read_schema_revision(connection)
    except SQLAlchemyError as error:
        msg = (
            "cannot connect to the simulation database: "
            f"{database_error_summary(error)}"
        )
        raise CommandRefusedError(msg) from None
    if role != RUNTIME_ROLE:
        msg = (
            f"connected as {role}, "
            f"but migration provisioning connects as {RUNTIME_ROLE}"
        )
        raise CommandRefusedError(msg)
    if revision != HEAD_REVISION:
        msg = (
            f"the simulation database is at revision {revision}, "
            f"but this code expects {HEAD_REVISION}"
        )
        raise CommandRefusedError(msg)


def _summary(result: ProvisioningResult) -> str:
    return json.dumps(
        {
            "dry_run": result.dry_run,
            "new_simulations": result.new_simulations,
            "existing_simulations": result.existing_simulations,
            "new_reports": result.new_reports,
            "existing_reports": result.existing_reports,
            "failures": [asdict(failure) for failure in result.failures],
        },
        indent=2,
    )


def _write_mapping(output: Path, result: ProvisioningResult) -> None:
    content = ReportMappingFile.from_result(result).model_dump_json(indent=2)
    # Mode "x" fails rather than replace a file created since the check.
    with output.open("x", encoding="utf-8") as mapping_file:
        mapping_file.write(content + "\n")


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    """Run the command and return its exit status."""
    arguments = _parse_arguments(argv)
    try:
        if arguments.output is not None:
            _check_output_path(arguments.output)
        definitions = _read_definitions(arguments.definitions)
        engine = _runtime_engine(os.environ if environ is None else environ)
    except CommandRefusedError as refusal:
        sys.stderr.write(f"{refusal}\n")
        return EXIT_REFUSED
    try:
        _check_database(engine)
        result = provision_reports(
            definitions,
            SimulationStore(engine),
            ReportStore(engine),
            dry_run=arguments.dry_run,
        )
    except CommandRefusedError as refusal:
        sys.stderr.write(f"{refusal}\n")
        return EXIT_REFUSED
    except SQLAlchemyError as error:
        summary = database_error_summary(error)
        if arguments.dry_run:
            message = (
                f"the database stopped the dry run: {summary}\n"
                "The dry run wrote nothing.\n"
            )
        else:
            message = (
                f"the database stopped the run: {summary}\n"
                "No output file was written. The rows created so far are kept, "
                "and running the command again reuses them.\n"
            )
        sys.stderr.write(message)
        return EXIT_STOPPED
    finally:
        engine.dispose()
    sys.stdout.write(_summary(result) + "\n")
    if arguments.output is not None:
        try:
            _write_mapping(arguments.output, result)
        except OSError as error:
            sys.stderr.write(
                f"cannot write {arguments.output}: {error.strerror}\n"
                "The run finished, but no output file was written. Running the "
                "command again with a new --output reuses every row it created "
                "and writes the mapping.\n"
            )
            return EXIT_STOPPED
    return EXIT_REPORTS_FAILED if result.failures else EXIT_PROVISIONED

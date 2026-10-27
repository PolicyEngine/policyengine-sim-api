"""The migration provisioning command an operator runs."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from alembic import command
from migration_provisioning_support import (
    definitions_json,
    economy_report_definition,
    economy_simulation_definition,
    household_report_definition,
    household_simulation_definition,
)
from migration_support import alembic_config
from policyengine_simulation_persistence import ReportStore
from policyengine_simulation_persistence.migration_provisioning import (
    REPORT_MAPPING_SCHEMA_VERSION,
    MappedReport,
    ReportMappingFile,
)
from policyengine_simulation_persistence.migration_provisioning.command import (
    main,
)
from report_support import report_store
from simulation_support import simulation_store
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from pathlib import Path

MODULE = "policyengine_simulation_persistence.migration_provisioning"
URL_VARIABLE = "SIMULATION_DATABASE_URL"
# Nothing listens on port 1, so a connection is refused at once.
UNREACHABLE_URL = (
    "postgresql://simulation_runtime:not-a-real-password@127.0.0.1:1/postgres"
)


def _write_definitions(tmp_path: Path, *report_definitions) -> Path:
    path = tmp_path / "report-definitions.json"
    path.write_text(definitions_json(*report_definitions))
    return path


def _economy_report(rows, legacy_report_id=4512):
    return economy_report_definition(
        legacy_report_id,
        reform=economy_simulation_definition(9002, policy_id=str(rows.us_policy_id)),
    )


def _household_report(rows, legacy_report_id=4513, **overrides):
    household_id = str(rows.us_household_id)
    values = {
        "baseline": household_simulation_definition(9003, household_id=household_id)
    }
    values.update(overrides)
    return household_report_definition(legacy_report_id, **values)


def _run(definitions: Path, *options: str, database_url: str | None) -> int:
    environ = {} if database_url is None else {URL_VARIABLE: database_url}
    return main(["--definitions", str(definitions), *options], environ=environ)


def _summary(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out)


def test_the_module_runs_as_a_command() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", MODULE, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "--definitions" in completed.stdout
    assert "--dry-run" in completed.stdout


def test_a_run_needs_an_output_file(tmp_path: Path) -> None:
    definitions = _write_definitions(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        _run(definitions, database_url=None)

    assert exit_info.value.code == 2


def test_a_dry_run_writes_no_output_file(tmp_path: Path) -> None:
    definitions = _write_definitions(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        _run(
            definitions,
            "--dry-run",
            "--output",
            str(tmp_path / "out.json"),
            database_url=None,
        )

    assert exit_info.value.code == 2


def test_an_existing_output_file_is_never_overwritten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definitions = _write_definitions(tmp_path, economy_report_definition())
    output = tmp_path / "report-mapping.json"
    output.write_text("earlier mapping")

    exit_code = _run(definitions, "--output", str(output), database_url=UNREACHABLE_URL)

    assert exit_code == 2
    assert output.read_text() == "earlier mapping"
    assert "already exists" in capsys.readouterr().err


def test_the_output_file_needs_an_existing_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definitions = _write_definitions(tmp_path, economy_report_definition())

    exit_code = _run(
        definitions,
        "--output",
        str(tmp_path / "missing" / "report-mapping.json"),
        database_url=UNREACHABLE_URL,
    )

    assert exit_code == 2
    assert "is not a directory" in capsys.readouterr().err


def test_an_invalid_definitions_file_is_refused_with_each_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definitions = _write_definitions(
        tmp_path,
        economy_report_definition(country="USA"),
        economy_report_definition(4600, reform=None),
    )

    exit_code = _run(
        definitions,
        "--output",
        str(tmp_path / "report-mapping.json"),
        database_url=UNREACHABLE_URL,
    )

    error_output = capsys.readouterr().err
    assert exit_code == 2
    assert "is not a valid report definitions file" in error_output
    assert "reports.0.economy: Value error, country must be" in error_output
    assert "reports.1.economy.reform: Input should be an object" in error_output
    assert not (tmp_path / "report-mapping.json").exists()


def test_a_file_with_many_errors_lists_the_first_twenty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definitions = _write_definitions(
        tmp_path,
        *(
            economy_report_definition(legacy_report_id, country="USA")
            for legacy_report_id in range(1, 26)
        ),
    )

    exit_code = _run(definitions, "--dry-run", database_url=UNREACHABLE_URL)

    error_lines = capsys.readouterr().err.splitlines()
    assert exit_code == 2
    assert len([line for line in error_lines if "country must be" in line]) == 20
    assert error_lines[-1] == "  and 5 more"


def test_a_file_that_is_not_json_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definitions = tmp_path / "report-definitions.json"
    definitions.write_text("{not json")

    exit_code = _run(definitions, "--dry-run", database_url=UNREACHABLE_URL)

    assert exit_code == 2
    assert "file: Invalid JSON" in capsys.readouterr().err


def test_a_missing_definitions_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _run(
        tmp_path / "missing.json", "--dry-run", database_url=UNREACHABLE_URL
    )

    assert exit_code == 2
    assert "cannot read" in capsys.readouterr().err


def test_the_database_url_comes_from_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definitions = _write_definitions(tmp_path, economy_report_definition())

    exit_code = _run(definitions, "--dry-run", database_url=None)

    assert exit_code == 2
    assert f"{URL_VARIABLE} is not set" in capsys.readouterr().err


def test_a_malformed_database_url_is_refused_without_repeating_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definitions = _write_definitions(tmp_path, economy_report_definition())

    exit_code = _run(
        definitions,
        "--dry-run",
        database_url="postgres://simulation_runtime:hunter2@db.example/postgres",
    )

    error_output = capsys.readouterr().err
    assert exit_code == 2
    assert "must use postgresql://" in error_output
    assert "hunter2" not in error_output


def test_an_unreachable_database_is_refused_without_its_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definitions = _write_definitions(tmp_path, economy_report_definition())

    exit_code = _run(definitions, "--dry-run", database_url=UNREACHABLE_URL)

    error_output = capsys.readouterr().err
    assert exit_code == 2
    assert "cannot connect to the simulation database" in error_output
    assert "not-a-real-password" not in error_output
    assert "\n" not in error_output.rstrip("\n")


@pytest.mark.postgres
def test_a_run_writes_the_mapping_of_each_v1_report(
    tmp_path: Path, migrated_database, public_api_rows, capsys
) -> None:
    definitions = _write_definitions(
        tmp_path, _economy_report(public_api_rows), _household_report(public_api_rows)
    )
    output = tmp_path / "report-mapping.json"

    exit_code = _run(
        definitions, "--output", str(output), database_url=migrated_database.runtime_url
    )

    reports = report_store(migrated_database)
    economy = reports.get_by_legacy_report_id(4512)
    household = reports.get_by_legacy_report_id(4513)
    assert economy is not None
    assert household is not None
    mapping = ReportMappingFile.model_validate_json(output.read_text())
    assert mapping == ReportMappingFile(
        schema_version=REPORT_MAPPING_SCHEMA_VERSION,
        reports=[
            MappedReport(legacy_report_id=4512, report_id=economy.id, created=True),
            MappedReport(legacy_report_id=4513, report_id=household.id, created=True),
        ],
        failures=[],
    )
    assert exit_code == 0
    assert _summary(capsys) == {
        "dry_run": False,
        "new_simulations": 3,
        "existing_simulations": 0,
        "new_reports": 2,
        "existing_reports": 0,
        "failures": [],
    }


@pytest.mark.postgres
def test_running_again_maps_the_same_reports(
    tmp_path: Path, migrated_database, public_api_rows
) -> None:
    definitions = _write_definitions(tmp_path, _economy_report(public_api_rows))
    first_output = tmp_path / "first.json"
    second_output = tmp_path / "second.json"
    _run(
        definitions,
        "--output",
        str(first_output),
        database_url=migrated_database.runtime_url,
    )

    exit_code = _run(
        definitions,
        "--output",
        str(second_output),
        database_url=migrated_database.runtime_url,
    )

    first = ReportMappingFile.model_validate_json(first_output.read_text())
    second = ReportMappingFile.model_validate_json(second_output.read_text())
    assert exit_code == 0
    assert [mapped.report_id for mapped in second.reports] == [
        mapped.report_id for mapped in first.reports
    ]
    assert [mapped.created for mapped in second.reports] == [False]


@pytest.mark.postgres
def test_failed_reports_are_listed_and_the_run_exits_with_one(
    tmp_path: Path, migrated_database, public_api_rows, capsys
) -> None:
    definitions = _write_definitions(
        tmp_path,
        _economy_report(public_api_rows),
        _household_report(
            public_api_rows,
            baseline=household_simulation_definition(
                9003, household_id=str(public_api_rows.uk_household_id)
            ),
        ),
    )
    output = tmp_path / "report-mapping.json"

    exit_code = _run(
        definitions, "--output", str(output), database_url=migrated_database.runtime_url
    )

    mapping = ReportMappingFile.model_validate_json(output.read_text())
    assert exit_code == 1
    assert [mapped.legacy_report_id for mapped in mapping.reports] == [4512]
    (failure,) = mapping.failures
    assert failure.legacy_report_id == 4513
    assert "fk_simulations_household_country" in failure.reason
    assert _summary(capsys)["failures"] == [
        {"legacy_report_id": 4513, "reason": failure.reason}
    ]


@pytest.mark.postgres
def test_a_dry_run_prints_what_a_run_would_do(
    tmp_path: Path, migrated_database, public_api_rows, capsys
) -> None:
    definitions = _write_definitions(tmp_path, _economy_report(public_api_rows))

    exit_code = _run(
        definitions, "--dry-run", database_url=migrated_database.runtime_url
    )

    assert exit_code == 0
    assert _summary(capsys) == {
        "dry_run": True,
        "new_simulations": 2,
        "existing_simulations": 0,
        "new_reports": 1,
        "existing_reports": 0,
        "failures": [],
    }
    assert report_store(migrated_database).get_by_legacy_report_id(4512) is None
    assert list(tmp_path.iterdir()) == [definitions]


@pytest.mark.postgres
def test_only_the_runtime_role_may_run_it(
    tmp_path: Path, migrated_database, public_api_rows, capsys
) -> None:
    definitions = _write_definitions(tmp_path, _economy_report(public_api_rows))
    output = tmp_path / "report-mapping.json"

    exit_code = _run(
        definitions,
        "--output",
        str(output),
        database_url=migrated_database.migrator_url,
    )

    assert exit_code == 2
    assert "connected as simulation_migrator" in capsys.readouterr().err
    assert not output.exists()
    assert report_store(migrated_database).get_by_legacy_report_id(4512) is None


@pytest.mark.postgres
def test_the_database_must_be_at_this_code_s_revision(
    tmp_path: Path, disposable_database, capsys
) -> None:
    command.upgrade(alembic_config(), "0001")
    definitions = _write_definitions(tmp_path, economy_report_definition())

    exit_code = _run(
        definitions, "--dry-run", database_url=disposable_database.runtime_url
    )

    assert exit_code == 2
    assert (
        "the simulation database is at revision 0001, but this code expects 0002"
        in capsys.readouterr().err
    )


@pytest.mark.postgres
def test_a_run_the_database_stops_writes_no_mapping_and_exits_with_three(
    tmp_path: Path,
    migrated_database,
    public_api_rows,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lose_the_connection(store, new_report):
        statement = "INSERT INTO simulation.reports"
        raise OperationalError(
            statement, {}, ConnectionError("server closed the connection")
        )

    monkeypatch.setattr(ReportStore, "create_migrated", lose_the_connection)
    definitions = _write_definitions(tmp_path, _economy_report(public_api_rows))
    output = tmp_path / "report-mapping.json"

    exit_code = _run(
        definitions, "--output", str(output), database_url=migrated_database.runtime_url
    )

    error_output = capsys.readouterr().err
    assert exit_code == 3
    assert "server closed the connection" in error_output
    assert "running the command again reuses them" in error_output
    assert not output.exists()
    # The simulations created before the stop stay, keyed by their v1 IDs.
    assert (
        simulation_store(migrated_database).get_by_legacy_simulation_id(9001)
        is not None
    )


@pytest.mark.postgres
def test_an_output_file_that_cannot_be_written_exits_with_three(
    tmp_path: Path,
    migrated_database,
    public_api_rows,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from policyengine_simulation_persistence.migration_provisioning import (
        command as command_module,
    )

    output = tmp_path / "report-mapping.json"
    provision_reports = command_module.provision_reports

    def provision_while_another_process_writes_the_output(*arguments, **options):
        result = provision_reports(*arguments, **options)
        output.write_text("written meanwhile")
        return result

    monkeypatch.setattr(
        command_module,
        "provision_reports",
        provision_while_another_process_writes_the_output,
    )
    definitions = _write_definitions(tmp_path, _economy_report(public_api_rows))

    exit_code = _run(
        definitions, "--output", str(output), database_url=migrated_database.runtime_url
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert output.read_text() == "written meanwhile"
    assert f"cannot write {output}" in captured.err
    assert "with a new --output" in captured.err
    assert json.loads(captured.out)["new_reports"] == 1


@pytest.mark.postgres
def test_a_dry_run_the_database_stops_says_it_wrote_nothing(
    tmp_path: Path,
    migrated_database,
    public_api_rows,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lose_the_connection(store, legacy_report_id):
        statement = "SELECT FROM simulation.reports"
        raise OperationalError(
            statement, {}, ConnectionError("server closed the connection")
        )

    monkeypatch.setattr(ReportStore, "get_by_legacy_report_id", lose_the_connection)
    definitions = _write_definitions(tmp_path, _economy_report(public_api_rows))

    exit_code = _run(
        definitions, "--dry-run", database_url=migrated_database.runtime_url
    )

    error_output = capsys.readouterr().err
    assert exit_code == 3
    assert "the database stopped the dry run: server closed the connection" in (
        error_output
    )
    assert "The dry run wrote nothing." in error_output
    assert "kept" not in error_output

"""Guard sim.persistence.orm-only over every source directory.

Database code declares tables only as SQLAlchemy ORM mapped classes and runs
every statement through an ORM Session against them;
docs/engineering/skills/database-access.md#orm explains the rule. The checks
live in orm_access_support.py, and any finding fails. The source directories
are the ones sim.boundaries.sqlalchemy-and-alembic-only scans, whose
test_every_source_directory_is_scanned checks that each one contributes files.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import TYPE_CHECKING

import tomllib
from orm_access_support import (
    CORE_CONNECTION,
    CORE_TABLE_DECLARATION,
    RAW_SQL,
    UNMAPPED_TABLE,
    orm_access_findings,
    unmapped_table_findings,
    unmapped_tables_in_packages,
)
from test_database_access_boundaries import REPOSITORY_ROOT, SOURCE_ROOT_PATTERNS

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

FIXTURES_ROOT = REPOSITORY_ROOT / "rules" / "fixtures" / "sim.persistence.orm-only"
PROJECT_PATTERNS = ("projects/*/pyproject.toml", "libs/*/pyproject.toml")
# The libraries that declare tables; every table they hold needs a mapped class.
TABLE_PACKAGES = (
    "policyengine_simulation_persistence",
    "policyengine_stage12_persistence",
)


def _sources() -> dict[str, str]:
    return {
        source_file.relative_to(REPOSITORY_ROOT).as_posix(): source_file.read_text(
            encoding="utf-8"
        )
        for pattern in SOURCE_ROOT_PATTERNS
        for source_root in REPOSITORY_ROOT.glob(pattern)
        for source_file in sorted(source_root.rglob("*.py"))
    }


def _load_fixture(fixture_name: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = FIXTURES_ROOT / fixture_name
    spec = importlib.util.spec_from_file_location(f"orm_only_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # SQLAlchemy resolves a mapped class's annotations in its module, which it
    # finds in sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _fixture_findings(fixture_name: str) -> list[tuple[int, str, str]]:
    fixture = FIXTURES_ROOT / fixture_name
    findings = orm_access_findings({fixture_name: fixture.read_text(encoding="utf-8")})
    return [(finding.line, finding.check, finding.source) for finding in findings]


def test_database_code_uses_only_the_orm() -> None:
    findings = orm_access_findings(_sources())

    assert [str(finding) for finding in findings] == [], (
        "Code reaches the database without the ORM; use a mapped class and a "
        "Session (docs/engineering/skills/database-access.md#orm). For a .table "
        "finding on converted code, name the attribute after what it holds, such "
        "as model"
    )


def test_every_table_is_mapped_by_a_class() -> None:
    findings = unmapped_tables_in_packages(TABLE_PACKAGES, REPOSITORY_ROOT)

    assert [str(finding) for finding in findings] == []


def test_no_project_installs_sqlmodel() -> None:
    """The rule's ORM is SQLAlchemy's own, so no installable project locks SQLModel."""
    projects_with_sqlmodel = []
    for pattern in PROJECT_PATTERNS:
        for pyproject in sorted(REPOSITORY_ROOT.glob(pattern)):
            lock = tomllib.loads(
                (pyproject.parent / "uv.lock").read_text(encoding="utf-8")
            )
            if any(package["name"] == "sqlmodel" for package in lock["package"]):
                projects_with_sqlmodel.append(
                    pyproject.parent.relative_to(REPOSITORY_ROOT).as_posix()
                )

    assert projects_with_sqlmodel == []


def test_rule_proof_orm_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proof for rule sim.persistence.orm-only."""
    assert _fixture_findings("bad_household_job_records.py") == [
        (15, CORE_CONNECTION, "sqlalchemy.create_engine"),
        (20, CORE_CONNECTION, "sqlalchemy.engine.Connection"),
        (20, CORE_CONNECTION, "sqlalchemy.engine.Engine"),
        (22, CORE_TABLE_DECLARATION, "MetaData(...)"),
        (23, CORE_TABLE_DECLARATION, "Table('household_jobs', ...)"),
        (26, CORE_TABLE_DECLARATION, "Column('id', ...)"),
        (27, CORE_TABLE_DECLARATION, "Column('status', ...)"),
        (28, CORE_TABLE_DECLARATION, "Column('modal_call_id', ...)"),
        (39, CORE_CONNECTION, "connection.execute(...)"),
        (48, CORE_CONNECTION, "connection.execute(...)"),
        (61, CORE_CONNECTION, "engine.connect()"),
        (62, CORE_CONNECTION, "connection.execute(...)"),
        (
            63,
            RAW_SQL,
            "text(\"SELECT count(*) FROM household_jobs WHERE status = 'pending'\")",
        ),
    ]
    assert _fixture_findings("good_household_job_records.py") == []
    bad_module = _load_fixture("bad_household_job_records.py", monkeypatch)
    good_module = _load_fixture("good_household_job_records.py", monkeypatch)
    assert [
        (finding.check, finding.source)
        for finding in unmapped_table_findings([bad_module], REPOSITORY_ROOT)
    ] == [(UNMAPPED_TABLE, "public.household_jobs")]
    assert unmapped_table_findings([good_module], REPOSITORY_ROOT) == []

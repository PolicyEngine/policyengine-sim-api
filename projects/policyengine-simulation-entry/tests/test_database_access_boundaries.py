"""Guard sim.boundaries.sqlalchemy-and-alembic-only over every source directory.

Code connects to databases only through SQLAlchemy, and runtime code never
changes the schema; docs/engineering/skills/database-access.md explains the
rule. The checks live in database_access_support.py.
"""

from __future__ import annotations

from pathlib import Path

from database_access_support import (
    DRIVER_IMPORT,
    SCHEMA_CHANGE_CALL,
    database_access_findings,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_ROOT = (
    REPOSITORY_ROOT
    / "rules"
    / "fixtures"
    / "sim.boundaries.sqlalchemy-and-alembic-only"
)
# Every directory that holds non-test source. The executor's src/modal is
# under projects/*/src.
SOURCE_ROOT_PATTERNS = ("projects/*/src", "libs/*/src", ".github/scripts")
# Directories the rule names, each of which must contribute scanned files.
REQUIRED_SOURCE_DIRECTORIES = (
    "projects/policyengine-simulation-entry/src",
    "projects/policyengine-simulation-executor/src/modal",
    "projects/policyengine-simulation-executor/src/policyengine_simulation_executor",
    "projects/policyengine-simulation-gateway/src",
    "libs/policyengine-fastapi/src",
    "libs/policyengine-simulation-contract/src",
    "libs/policyengine-simulation-observability/src",
    "libs/policyengine-simulation-persistence/src",
    "libs/policyengine-stage12-persistence/src",
    ".github/scripts",
)


def _source_files() -> list[Path]:
    return sorted(
        source_file
        for pattern in SOURCE_ROOT_PATTERNS
        for source_root in REPOSITORY_ROOT.glob(pattern)
        for source_file in source_root.rglob("*.py")
    )


def _relative_name(source_file: Path) -> str:
    return source_file.relative_to(REPOSITORY_ROOT).as_posix()


def _fixture_findings(fixture_name: str) -> list[tuple[int, str, str]]:
    fixture = FIXTURES_ROOT / fixture_name
    findings = database_access_findings(
        {fixture_name: fixture.read_text(encoding="utf-8")}
    )
    return [(finding.line, finding.check, finding.source) for finding in findings]


def test_source_code_uses_sqlalchemy_and_never_changes_the_schema() -> None:
    sources = {
        _relative_name(source_file): source_file.read_text(encoding="utf-8")
        for source_file in _source_files()
    }

    findings = database_access_findings(sources)

    assert [str(finding) for finding in findings] == []


def test_every_source_directory_is_scanned() -> None:
    scanned = [_relative_name(source_file) for source_file in _source_files()]
    globbed_roots = [
        _relative_name(source_root)
        for pattern in SOURCE_ROOT_PATTERNS
        for source_root in REPOSITORY_ROOT.glob(pattern)
    ]

    unscanned = [
        directory
        for directory in sorted({*REQUIRED_SOURCE_DIRECTORIES, *globbed_roots})
        if not any(name.startswith(f"{directory}/") for name in scanned)
    ]

    assert unscanned == []


def test_rule_proof_sqlalchemy_and_alembic_only() -> None:
    """Proof for rule sim.boundaries.sqlalchemy-and-alembic-only."""
    assert _fixture_findings("bad_database_store.py") == [
        (16, DRIVER_IMPORT, "import psycopg"),
        (17, DRIVER_IMPORT, "from psycopg.rows import dict_row"),
        (57, SCHEMA_CHANGE_CALL, "metadata.create_all(engine)"),
    ]
    assert _fixture_findings("good_database_store.py") == []

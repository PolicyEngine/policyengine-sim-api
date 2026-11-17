"""Guard sim.persistence.orm-only over every source directory.

Database code declares tables only as SQLAlchemy ORM mapped classes and runs
every statement through an ORM Session against them;
docs/engineering/skills/database-access.md#orm explains the rule. The checks
live in orm_access_support.py.

Until every module is converted, the findings listed in
rules/baselines/orm-only.txt are tolerated, and only those: a finding missing
from it fails, and so does an entry that no longer matches a finding, so the
list only shrinks. The source directories are the ones
sim.boundaries.sqlalchemy-and-alembic-only scans, whose
test_every_source_directory_is_scanned checks that each one contributes files.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from typing import TYPE_CHECKING

from orm_access_support import (
    CORE_COLUMN_ACCESS,
    CORE_CONNECTION,
    CORE_TABLE_DECLARATION,
    RAW_SQL,
    STATEMENT_NOT_ON_ENTITY,
    UNMAPPED_TABLE,
    orm_access_findings,
    read_baseline,
    unmapped_table_findings,
    unmapped_tables_in_packages,
)
from test_database_access_boundaries import REPOSITORY_ROOT, SOURCE_ROOT_PATTERNS

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import ModuleType

    import pytest
    from orm_access_support import Finding

BASELINE = REPOSITORY_ROOT / "rules" / "baselines" / "orm-only.txt"
FIXTURES_ROOT = REPOSITORY_ROOT / "rules" / "fixtures" / "sim.persistence.orm-only"
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


def _tolerated(*, unmapped: bool) -> Counter[str]:
    marker = f": {UNMAPPED_TABLE}: "
    return Counter(
        {
            entry: count
            for entry, count in read_baseline(BASELINE).items()
            if (marker in entry) == unmapped
        }
    )


def _assert_only_tolerated(
    findings: Iterable[Finding], tolerated: Counter[str]
) -> None:
    found = Counter(finding.baseline_entry for finding in findings)
    new_findings = sorted((found - tolerated).elements())
    fixed_entries = sorted((tolerated - found).elements())
    assert new_findings == [], (
        "Code reaches the database without the ORM; use a mapped class and a "
        "Session (docs/engineering/skills/database-access.md#orm) instead of "
        f"adding to {BASELINE.name}"
    )
    assert fixed_entries == [], (
        f"These {BASELINE.name} entries no longer match a finding; remove them"
    )


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

    _assert_only_tolerated(findings, _tolerated(unmapped=False))


def test_every_table_is_mapped_by_a_class() -> None:
    findings = unmapped_tables_in_packages(TABLE_PACKAGES, REPOSITORY_ROOT)

    _assert_only_tolerated(findings, _tolerated(unmapped=True))


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
        (40, STATEMENT_NOT_ON_ENTITY, "insert(household_jobs)"),
        (49, STATEMENT_NOT_ON_ENTITY, "update(household_jobs)"),
        (51, CORE_COLUMN_ACCESS, "household_jobs.c"),
        (52, CORE_COLUMN_ACCESS, "household_jobs.c"),
        (55, CORE_COLUMN_ACCESS, "household_jobs.c"),
        (61, CORE_CONNECTION, "engine.connect()"),
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

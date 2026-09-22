"""Keep Stage 12 database schema ownership outside simulation runtimes."""

from __future__ import annotations

from pathlib import Path
import re

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_SOURCE_ROOTS = (
    REPOSITORY_ROOT / "libs/policyengine-stage12-persistence/src",
    REPOSITORY_ROOT / "projects/policyengine-simulation-entry/src",
    REPOSITORY_ROOT / "projects/policyengine-simulation-executor/src",
)
DDL_PATTERN = re.compile(
    r"\b(?:CREATE|ALTER|DROP|TRUNCATE)\s+"
    r"(?:TABLE|TYPE|SCHEMA|DATABASE)\b|\bcreate_all\s*\(",
    flags=re.IGNORECASE,
)


def test_simulation_runtimes_contain_no_schema_ddl() -> None:
    violations: list[str] = []
    for source_root in RUNTIME_SOURCE_ROOTS:
        for path in source_root.rglob("*.py"):
            match = DDL_PATTERN.search(path.read_text(encoding="utf-8"))
            if match is not None:
                violations.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}: {match.group(0)}"
                )

    assert violations == []


def test_simulation_repository_has_no_schema_migration_chain() -> None:
    assert not list(REPOSITORY_ROOT.glob("alembic*.ini"))
    assert not list(REPOSITORY_ROOT.rglob("alembic/versions"))
    assert not list(REPOSITORY_ROOT.rglob("migrations/versions"))


def test_stage12_runtime_cannot_create_production_user_associations() -> None:
    forbidden = {
        "user_simulation",
        "user_simulations",
        "user_report",
        "user_reports",
    }

    for source_root in RUNTIME_SOURCE_ROOTS:
        for path in source_root.rglob("stage12_*.py"):
            source = path.read_text(encoding="utf-8").lower()
            assert not any(name in source for name in forbidden), path

"""Keep Stage 12 database schema ownership outside simulation runtimes.

DDL_PATTERN is shared with sim.boundaries.sqlalchemy-and-alembic-only, which
matches it against the string literals of every source directory; see
database_access_support.py. The Simulation Entrypoint's own schema has its own
ownership rules, checked in
libs/policyengine-simulation-persistence/tests/test_ownership_boundaries.py.
"""

from __future__ import annotations

from pathlib import Path

from database_access_support import DDL_PATTERN

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_SOURCE_ROOTS = (
    REPOSITORY_ROOT / "libs/policyengine-stage12-persistence/src",
    REPOSITORY_ROOT / "projects/policyengine-simulation-entry/src",
    REPOSITORY_ROOT / "projects/policyengine-simulation-executor/src",
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


def test_rule_proof_stage12_no_schema_ddl() -> None:
    """Proof for rule sim.stage12.no-schema-ddl (rules/registry.toml)."""
    fixtures = REPOSITORY_ROOT / "rules/fixtures/sim.stage12.no-schema-ddl"
    bad = (fixtures / "bad_stage12_runtime.py").read_text(encoding="utf-8")
    good = (fixtures / "good_stage12_runtime.py").read_text(encoding="utf-8")
    assert DDL_PATTERN.search(bad) is not None
    assert DDL_PATTERN.search(good) is None


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

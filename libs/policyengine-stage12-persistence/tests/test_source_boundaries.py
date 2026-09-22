from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src"


def test_persistence_package_contains_no_raw_sql_or_schema_operations() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in PACKAGE_ROOT.rglob("*.py")
    )

    assert "text(" not in source
    assert "exec_driver_sql" not in source
    assert ".cursor(" not in source
    assert "create_all(" not in source
    assert "drop_all(" not in source

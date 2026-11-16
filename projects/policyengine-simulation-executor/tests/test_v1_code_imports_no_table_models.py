"""The v1 ``run_simulation`` code imports neither persistence library.

The simulation service and the v2 Modal runtime share one library of the
Public API's table models, ``policyengine-simulation-persistence``, which the
Stage 12 library builds on. The v1 code, which will be removed, stays apart
from both: the old gateway, and the v1 Modal app (``src/modal/app.py``) with
every module of this project it imports, however indirectly and wherever in
the module the import is.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
GATEWAY_SOURCE_ROOT = PROJECT_ROOT.parent / "policyengine-simulation-gateway" / "src"
V1_MODAL_APP = SOURCE_ROOT / "modal" / "app.py"
# Modules of this project, found as files under src.
PROJECT_PACKAGES = ("src.modal", "policyengine_simulation_executor")
TABLE_MODEL_LIBRARIES = (
    "policyengine_simulation_persistence",
    "policyengine_stage12_persistence",
)


def _module_name(path: Path) -> str:
    parts = path.relative_to(SOURCE_ROOT).with_suffix("").parts
    if parts[0] == "modal":
        parts = ("src", *parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _module_path(module_name: str) -> Path | None:
    """Return the file of one of this project's modules, or None for another's."""
    if not module_name.startswith(PROJECT_PACKAGES):
        return None
    parts = module_name.split(".")
    if parts[0] == "src":
        parts = parts[1:]
    candidate = SOURCE_ROOT.joinpath(*parts)
    for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
        if path.is_file():
            return path
    return None


def _imported_modules(path: Path) -> set[str]:
    """Return every module ``path`` imports, and each package it imports from."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _module_name(path)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = (
                    package.rsplit(".", node.level - 1)[0]
                    if node.level > 1
                    else package
                )
                base = f"{anchor}.{base}" if base else anchor
            imported.add(base)
            # ``from package import module`` imports a submodule too.
            imported.update(f"{base}.{alias.name}" for alias in node.names)
    return imported


def _v1_modal_imports() -> dict[str, set[str]]:
    """Return, by module, what the v1 Modal app and its project modules import."""
    pending = [V1_MODAL_APP]
    imports_by_module: dict[str, set[str]] = {}
    while pending:
        path = pending.pop()
        module_name = _module_name(path)
        if module_name in imports_by_module:
            continue
        imported = _imported_modules(path)
        imports_by_module[module_name] = imported
        pending.extend(
            module_path
            for name in imported
            if (module_path := _module_path(name)) is not None
        )
    return imports_by_module


def _table_model_imports(imports_by_module: dict[str, set[str]]) -> list[str]:
    return sorted(
        f"{module_name}: {name}"
        for module_name, imported in imports_by_module.items()
        for name in imported
        if name.split(".")[0] in TABLE_MODEL_LIBRARIES
    )


def test_the_v1_modal_app_and_its_modules_import_no_table_model_library() -> None:
    imports_by_module = _v1_modal_imports()

    # The walk follows the app's own imports, not only the file itself.
    assert "src.modal._image_setup" in imports_by_module
    assert _table_model_imports(imports_by_module) == []


def test_the_old_gateway_imports_no_table_model_library() -> None:
    gateway_files = sorted(GATEWAY_SOURCE_ROOT.rglob("*.py"))
    imports_by_module = {
        str(path.relative_to(GATEWAY_SOURCE_ROOT)): _gateway_imports(path)
        for path in gateway_files
    }

    assert gateway_files
    assert _table_model_imports(imports_by_module) == []


def _gateway_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            imported.add(node.module or "")
    return imported

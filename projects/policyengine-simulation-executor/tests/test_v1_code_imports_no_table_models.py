"""The v1 ``run_simulation`` code imports neither persistence library.

The simulation service and the v2 Modal runtime share one library of the
Public API's table models, ``policyengine-simulation-persistence``, which the
Stage 12 library builds on. The v1 code, which will be removed, stays apart
from both: the old gateway, and the v1 Modal app (``src/modal/app.py``) with
every module of this repository it imports, however indirectly and wherever
in the module the import is.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
GATEWAY_SOURCE_ROOT = PROJECT_ROOT.parent / "policyengine-simulation-gateway" / "src"
V1_MODAL_APP = SOURCE_ROOT / "modal" / "app.py"
TABLE_MODEL_LIBRARIES = (
    "policyengine_simulation_persistence",
    "policyengine_stage12_persistence",
)


def _first_party_packages(source_roots: tuple[Path, ...]) -> frozenset[str]:
    """Return the packages under ``source_roots``, but the table-model libraries."""
    packages = {
        path.name
        for root in source_roots
        for path in root.iterdir()
        if (path / "__init__.py").is_file() and path.name != "modal"
    }
    return frozenset(packages - set(TABLE_MODEL_LIBRARIES))


def _module_name(path: Path, source_roots: tuple[Path, ...]) -> str:
    root = next(root for root in source_roots if path.is_relative_to(root))
    parts = path.relative_to(root).with_suffix("").parts
    # The Modal entry files are imported as src.modal, from the project.
    if parts[0] == "modal":
        parts = ("src", *parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _module_path(
    module_name: str,
    source_roots: tuple[Path, ...],
    first_party: frozenset[str],
) -> Path | None:
    """Return the file of a module of this repository, or None for another's."""
    parts = module_name.split(".")
    if parts[:2] == ["src", "modal"]:
        parts = parts[1:]
    elif parts[0] not in first_party:
        return None
    for root in source_roots:
        candidate = root.joinpath(*parts)
        for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
            if path.is_file():
                return path
    return None


def _imported_modules(path: Path, source_roots: tuple[Path, ...]) -> set[str]:
    """Return every module ``path`` imports, and each package it imports from."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _module_name(path, source_roots)
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


def _with_parent_packages(module_name: str) -> list[str]:
    """Return ``module_name`` and each package Python imports before it."""
    parts = module_name.split(".")
    return [".".join(parts[:count]) for count in range(1, len(parts) + 1)]


def imports_reached(entry: Path, source_roots: tuple[Path, ...]) -> dict[str, set[str]]:
    """Return, by module, what ``entry`` and every module it reaches import.

    Importing a module runs each of its parent packages' ``__init__`` first,
    so those are reached too. It follows only modules under ``source_roots``,
    never into the table-model libraries, whose imports are what the tests
    look for.
    """
    first_party = _first_party_packages(source_roots)
    pending = [entry]
    imports_by_module: dict[str, set[str]] = {}
    while pending:
        path = pending.pop()
        module_name = _module_name(path, source_roots)
        if module_name in imports_by_module:
            continue
        imported = _imported_modules(path, source_roots)
        imports_by_module[module_name] = imported
        pending.extend(
            module_path
            for name in imported
            for reached_name in _with_parent_packages(name)
            if (module_path := _module_path(reached_name, source_roots, first_party))
            is not None
        )
    return imports_by_module


def table_model_imports(imports_by_module: dict[str, set[str]]) -> list[str]:
    return sorted(
        f"{module_name}: {name}"
        for module_name, imported in imports_by_module.items()
        for name in imported
        if name.split(".")[0] in TABLE_MODEL_LIBRARIES
    )


def _write_module(root: Path, relative_path: str, source: str = "") -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_an_import_through_a_package_counts_its_package_too(tmp_path) -> None:
    """Python runs a package's ``__init__`` before any module in it."""
    project = tmp_path / "src"
    _write_module(project, "modal/__init__.py")
    _write_module(
        project,
        "modal/app.py",
        "from policyengine_simulation_executor.runtime.leaf import value\n",
    )
    _write_module(project, "policyengine_simulation_executor/__init__.py")
    _write_module(
        project,
        "policyengine_simulation_executor/runtime/__init__.py",
        "import policyengine_stage12_persistence\n",
    )
    _write_module(project, "policyengine_simulation_executor/runtime/leaf.py")

    reached = imports_reached(project / "modal" / "app.py", (project,))

    assert table_model_imports(reached) == [
        "policyengine_simulation_executor.runtime: policyengine_stage12_persistence"
    ]


def test_an_import_in_the_entry_modules_own_package_counts(tmp_path) -> None:
    """Importing ``src.modal.app`` runs ``src.modal``'s ``__init__`` first."""
    project = tmp_path / "src"
    _write_module(
        project, "modal/__init__.py", "import policyengine_simulation_persistence\n"
    )
    _write_module(project, "modal/app.py")

    reached = imports_reached(project / "modal" / "app.py", (project,))

    assert table_model_imports(reached) == [
        "src.modal: policyengine_simulation_persistence"
    ]


def test_an_import_through_a_first_party_library_counts(tmp_path) -> None:
    project = tmp_path / "project" / "src"
    library = tmp_path / "library" / "src"
    _write_module(project, "modal/__init__.py")
    _write_module(project, "modal/app.py", "import policyengine_shared_helpers\n")
    _write_module(
        library,
        "policyengine_shared_helpers/__init__.py",
        "from policyengine_simulation_persistence import tables\n",
    )

    reached = imports_reached(project / "modal" / "app.py", (project, library))

    assert table_model_imports(reached) == [
        "policyengine_shared_helpers: policyengine_simulation_persistence",
        "policyengine_shared_helpers: policyengine_simulation_persistence.tables",
    ]


def _repository_source_roots() -> tuple[Path, ...]:
    """Return this project's source and every library's, for the walk."""
    return (SOURCE_ROOT, *sorted((REPOSITORY_ROOT / "libs").glob("*/src")))


def test_the_v1_modal_app_and_its_modules_import_no_table_model_library() -> None:
    imports_by_module = imports_reached(V1_MODAL_APP, _repository_source_roots())

    # The walk follows the app's own imports, not only the file itself, and
    # into sim's libraries.
    assert "src.modal._image_setup" in imports_by_module
    assert "policyengine_simulation_contract" in imports_by_module
    assert table_model_imports(imports_by_module) == []


def test_the_old_gateway_imports_no_table_model_library() -> None:
    gateway_files = sorted(GATEWAY_SOURCE_ROOT.rglob("*.py"))
    imports_by_module = {
        str(path.relative_to(GATEWAY_SOURCE_ROOT)): _gateway_imports(path)
        for path in gateway_files
    }

    assert gateway_files
    assert table_model_imports(imports_by_module) == []


def _gateway_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            imported.add(node.module or "")
    return imported

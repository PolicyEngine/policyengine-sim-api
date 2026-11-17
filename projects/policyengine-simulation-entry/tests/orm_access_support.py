"""Checks behind sim.persistence.orm-only in rules/registry.toml.

Database code declares tables only as SQLAlchemy ORM mapped classes and runs
every statement through an ORM Session against them.
``orm_access_findings`` reads source, so the rule test runs it on the
repository and the proof test runs it on the fixtures under
``rules/fixtures/sim.persistence.orm-only/``. ``unmapped_table_findings``
imports modules and reports every table no mapped class maps, including
tables built in ways the source checks can't see.

The source checks resolve names through each module's own imports of
``sqlalchemy``: ``from sqlalchemy import text`` and ``import sqlalchemy as sa``
both count, and a ``text`` or ``update`` from another package doesn't. A
mapped class is a class deriving from ``DeclarativeBase``, directly or through
other classes in the scanned files; classes are recognised by name across all
files, since modules import their models from shared modules. Together the
checks leave no way to obtain a Core table or a Connection: no module may
build a table, read one through ``__table__``, or hold an engine outside a
library's ``engine.py``, so every ``execute`` left is a Session's. They don't
follow imports made through ``importlib`` or ``__import__``, or an engine
passed around without a type annotation.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from database_access_support import table_names

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from types import ModuleType

SQLALCHEMY_MODULE = "sqlalchemy"
SQLMODEL_MODULE = "sqlmodel"
# Core's table declarations. MetaData may be built only in a declarative base
# module, as the metadata of the library's DeclarativeBase.
TABLE_CONSTRUCTORS = frozenset({"Column", "MetaData", "Table"})
METADATA_CONSTRUCTOR = "MetaData"
DECLARATIVE_BASE_MODULE_NAME = "orm_base.py"
DECLARATIVE_BASES = frozenset({"DeclarativeBase", "DeclarativeBaseNoMeta"})
# The one module per library that may create and hold an engine, and hand out
# a sessionmaker.
SESSION_FACTORY_PATTERN = "libs/*/src/*/engine.py"
CONNECTION_NAMES = frozenset(
    {
        "AsyncConnection",
        "AsyncEngine",
        "Connection",
        "Engine",
        "create_async_engine",
        "create_engine",
        "engine_from_config",
    }
)
CONNECTION_METHODS = frozenset({"connect", "connection", "raw_connection"})
STATEMENT_FUNCTIONS = frozenset({"delete", "insert", "select", "update"})
ALIAS_FUNCTION = "aliased"
COLUMN_COLLECTION_ATTRIBUTE = "c"
COLUMNS_ATTRIBUTE = "columns"
TABLE_ATTRIBUTE = "__table__"
RAW_SQL_FUNCTIONS = frozenset({"literal_column", "text"})
RAW_SQL_METHODS = frozenset({"cursor", "exec_driver_sql"})
# A model's schema details that SQLAlchemy accepts only as SQL text: a server
# default and a partial index's condition.
SCHEMA_DETAIL_KEYWORDS = frozenset(
    {"postgresql_where", "server_default", "server_onupdate", "sqlite_where"}
)

CORE_TABLE_DECLARATION = "Core table declaration"
CORE_COLUMN_ACCESS = "Core column collection"
CORE_CONNECTION = "Core engine or connection"
STATEMENT_NOT_ON_ENTITY = "statement not on a mapped class"
RAW_SQL = "raw SQL"
SQLMODEL_IMPORT = "sqlmodel import"
UNMAPPED_TABLE = "table without a mapped class"


@dataclass(frozen=True)
class Finding:
    """One place where code reaches the database without the ORM."""

    file: str
    line: int
    check: str
    source: str

    @property
    def baseline_entry(self) -> str:
        """The finding as rules/baselines/orm-only.txt lists it, without its line."""
        return f"{self.file}: {self.check}: {self.source}"

    def __str__(self) -> str:
        """Describe the finding as ``file:line: check: source``."""
        return f"{self.file}:{self.line}: {self.check}: {self.source}"


def read_baseline(path: Path) -> Counter[str]:
    """Count each tolerated entry, skipping blank and ``#`` comment lines."""
    return Counter(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _matches_module(module_name: str, parent_module: str) -> bool:
    return module_name == parent_module or module_name.startswith(f"{parent_module}.")


def _sqlalchemy_imports(tree: ast.Module) -> dict[str, str]:
    """Map each name the module imports from sqlalchemy to its dotted name."""
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _matches_module(alias.name, SQLALCHEMY_MODULE):
                    continue
                if alias.asname:
                    imported[alias.asname] = alias.name
                else:
                    imported[alias.name.split(".")[0]] = SQLALCHEMY_MODULE
        elif (
            isinstance(node, ast.ImportFrom)
            and not node.level
            and node.module is not None
            and _matches_module(node.module, SQLALCHEMY_MODULE)
        ):
            for alias in node.names:
                imported[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return imported


def _qualified_name(node: ast.expr, imported: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return imported.get(node.id)
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value, imported)
        return None if parent is None else f"{parent}.{node.attr}"
    return None


def _sqlalchemy_name(node: ast.expr, imported: Mapping[str, str]) -> str:
    """Return the last part of the sqlalchemy name ``node`` refers to, or ``""``."""
    qualified = _qualified_name(node, imported)
    return qualified.rsplit(".", 1)[-1] if qualified else ""


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def mapped_class_names(trees: Iterable[ast.Module]) -> frozenset[str]:
    """Return the declarative bases and mapped classes the modules define."""
    classes = [
        node
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    ]
    mapped: set[str] = set()
    grew = True
    while grew:
        grew = False
        for class_node in classes:
            if class_node.name in mapped:
                continue
            if any(
                _base_name(base) in DECLARATIVE_BASES or _base_name(base) in mapped
                for base in class_node.bases
            ):
                mapped.add(class_node.name)
                grew = True
    return frozenset(mapped)


def _root(node: ast.expr) -> ast.expr:
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node


def _alias_names(tree: ast.Module, imported: Mapping[str, str]) -> frozenset[str]:
    """Return the names the module binds to ``aliased(...)`` of a mapped class."""
    return frozenset(
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and _sqlalchemy_name(node.value.func, imported) == ALIAS_FUNCTION
        for target in node.targets
        if isinstance(target, ast.Name)
    )


def _names_entity(
    target: ast.expr, imported: Mapping[str, str], entities: frozenset[str]
) -> bool:
    root = _root(target)
    if isinstance(root, ast.Name) and root.id in entities:
        return True
    # A SQLAlchemy expression, such as func.count() or exists(...).
    return (
        isinstance(target, ast.Call)
        and _qualified_name(target.func, imported) is not None
    )


def _call_summary(call: ast.Call) -> str:
    parts = [ast.unparse(call.args[0])] if call.args else []
    if len(call.args) > 1 or call.keywords:
        parts.append("...")
    return f"{ast.unparse(call.func)}({', '.join(parts)})"


def _schema_detail_calls(tree: ast.Module) -> set[int]:
    """Return the calls passed as a server default or a partial index's condition."""
    return {
        id(keyword.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg in SCHEMA_DETAIL_KEYWORDS
    }


@dataclass(frozen=True)
class _ModuleContext:
    file: str
    imported: Mapping[str, str]
    entities: frozenset[str]
    known_tables: frozenset[str]
    schema_details: set[int]

    @property
    def is_session_factory(self) -> bool:
        return PurePosixPath(self.file).match(SESSION_FACTORY_PATTERN)

    @property
    def is_declarative_base(self) -> bool:
        return PurePosixPath(self.file).name == DECLARATIVE_BASE_MODULE_NAME


def _import_findings(
    node: ast.Import | ast.ImportFrom, context: _ModuleContext
) -> Iterator[tuple[str, str]]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if _matches_module(alias.name, SQLMODEL_MODULE):
                yield SQLMODEL_IMPORT, alias.name
        return
    if node.level or node.module is None:
        return
    if _matches_module(node.module, SQLMODEL_MODULE):
        yield SQLMODEL_IMPORT, node.module
    elif (
        _matches_module(node.module, SQLALCHEMY_MODULE)
        and not context.is_session_factory
    ):
        for alias in node.names:
            if alias.name in CONNECTION_NAMES:
                yield CORE_CONNECTION, f"{node.module}.{alias.name}"


def _reads_core_columns(node: ast.Attribute, context: _ModuleContext) -> bool:
    if node.attr in {COLUMN_COLLECTION_ATTRIBUTE, TABLE_ATTRIBUTE}:
        return True
    return node.attr == COLUMNS_ATTRIBUTE and (
        _base_name(node.value) in context.known_tables
        or (
            isinstance(node.value, ast.Attribute) and node.value.attr == TABLE_ATTRIBUTE
        )
    )


def _attribute_findings(
    node: ast.Attribute, context: _ModuleContext
) -> Iterator[tuple[str, str]]:
    if _reads_core_columns(node, context):
        yield CORE_COLUMN_ACCESS, ast.unparse(node)
    elif not context.is_session_factory and (
        _sqlalchemy_name(node, context.imported) in CONNECTION_NAMES
    ):
        yield CORE_CONNECTION, str(_qualified_name(node, context.imported))


def _runs_raw_sql(node: ast.Call, function: str, context: _ModuleContext) -> bool:
    if function in RAW_SQL_FUNCTIONS:
        return id(node) not in context.schema_details
    return isinstance(node.func, ast.Attribute) and node.func.attr in RAW_SQL_METHODS


def _call_findings(
    node: ast.Call, context: _ModuleContext
) -> Iterator[tuple[str, str]]:
    function = _sqlalchemy_name(node.func, context.imported)
    if function in TABLE_CONSTRUCTORS and not (
        function == METADATA_CONSTRUCTOR and context.is_declarative_base
    ):
        yield CORE_TABLE_DECLARATION, _call_summary(node)
    elif function in STATEMENT_FUNCTIONS and (
        not node.args
        or not _names_entity(node.args[0], context.imported, context.entities)
    ):
        yield STATEMENT_NOT_ON_ENTITY, _call_summary(node)
    elif _runs_raw_sql(node, function, context):
        yield RAW_SQL, _call_summary(node)
    elif isinstance(node.func, ast.Attribute) and node.func.attr in CONNECTION_METHODS:
        yield CORE_CONNECTION, _call_summary(node)


def _module_findings(tree: ast.Module, context: _ModuleContext) -> Iterator[Finding]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            checks = list(_import_findings(node, context))
        elif isinstance(node, ast.Attribute):
            checks = list(_attribute_findings(node, context))
        elif isinstance(node, ast.Call):
            checks = list(_call_findings(node, context))
        else:
            checks = []
        for check, source in checks:
            yield Finding(context.file, getattr(node, "lineno", 0), check, source)


def orm_access_findings(sources: Mapping[str, str]) -> list[Finding]:
    """Return every place the sources reach the database without the ORM.

    ``sources`` maps a repository-relative file name to its source. Mapped
    classes and Core tables bound in one file are recognised in all of them.
    """
    trees = {file: ast.parse(source, filename=file) for file, source in sources.items()}
    entities = mapped_class_names(trees.values())
    known_tables = frozenset().union(*(table_names(tree) for tree in trees.values()))
    findings: list[Finding] = []
    for file, tree in trees.items():
        imported = _sqlalchemy_imports(tree)
        context = _ModuleContext(
            file=file,
            imported=imported,
            entities=entities | _alias_names(tree, imported),
            known_tables=known_tables,
            schema_details=_schema_detail_calls(tree),
        )
        findings.extend(_module_findings(tree, context))
    return sorted(findings, key=lambda finding: (finding.file, finding.line))


def _relative_file(path: str | None, repository_root: Path) -> str:
    if path is None:
        return "<unknown>"
    return Path(path).resolve().relative_to(repository_root).as_posix()


def unmapped_table_findings(
    modules: Iterable[ModuleType], repository_root: Path, *, location: str | None = None
) -> list[Finding]:
    """Return each table the modules hold that no mapped class maps.

    A module holds a table through a global bound to the table, to a MetaData,
    or to a declarative base or mapped class. Each table is reported once, at
    ``location`` when given, otherwise at the first module that holds it.
    """
    return _unmapped_findings(
        (module, location or _relative_file(module.__file__, repository_root))
        for module in modules
    )


def _unmapped_findings(
    placed_modules: Iterable[tuple[ModuleType, str]],
) -> list[Finding]:
    """Report each held table no mapped class of any of the modules maps."""
    from sqlalchemy import MetaData, Table
    from sqlalchemy.orm import DeclarativeBase

    held: dict[int, tuple[Table, str]] = {}
    mapped: set[int] = set()
    for module, place in placed_modules:
        for value in vars(module).values():
            if isinstance(value, Table):
                tables = [value]
            elif isinstance(value, MetaData):
                tables = list(value.tables.values())
            elif (
                isinstance(value, type)
                and issubclass(value, DeclarativeBase)
                and not value.__module__.startswith(SQLALCHEMY_MODULE)
            ):
                mapped.update(
                    id(mapper.local_table) for mapper in value.registry.mappers
                )
                tables = list(value.metadata.tables.values())
            else:
                tables = []
            for table in tables:
                held.setdefault(id(table), (table, place))
    findings = [
        Finding(place, 0, UNMAPPED_TABLE, table.fullname)
        for key, (table, place) in held.items()
        if key not in mapped
    ]
    return sorted(findings, key=lambda finding: (finding.file, finding.source))


def _package_modules(package_name: str) -> list[ModuleType]:
    package = importlib.import_module(package_name)
    names = sorted(
        module.name
        for module in pkgutil.walk_packages(package.__path__, f"{package_name}.")
    )
    return [package, *(importlib.import_module(name) for name in names)]


def unmapped_tables_in_packages(
    package_names: Iterable[str], repository_root: Path
) -> list[Finding]:
    """Return each table the packages' modules hold that no mapped class maps.

    A table is reported once, at the source directory of the first package
    that holds it, in the order given.
    """
    placed_modules: list[tuple[ModuleType, str]] = []
    for package_name in package_names:
        package_modules = _package_modules(package_name)
        directory = PurePosixPath(
            _relative_file(package_modules[0].__file__, repository_root)
        ).parent
        placed_modules.extend((module, str(directory)) for module in package_modules)
    return _unmapped_findings(placed_modules)

"""Checks behind sim.persistence.orm-only in rules/registry.toml.

Database code declares tables only as SQLAlchemy ORM mapped classes and runs
every statement through an ORM Session against them.
``orm_access_findings`` reads source, so the rule test runs it on the
repository and the proof test runs it on the fixtures under
``rules/fixtures/sim.persistence.orm-only/``. ``unmapped_table_findings``
reports every table that given modules hold and no mapped class maps, and
``unmapped_tables_in_packages`` imports whole packages for it, which finds
tables built in ways the source checks can't see.

The source checks look for Core rather than for the ORM, so any ORM statement
passes, including ones on expressions, labels, subqueries and aliases. They
resolve names through each module's own imports of ``sqlalchemy``: ``from
sqlalchemy import text`` and ``import sqlalchemy as sa`` both count, and a
``text`` or ``update`` from another package doesn't. A Core table is a name
bound to ``Table(...)`` or ``table(...)``, or annotated as ``Table``, in any
scanned file, or imported under another name; modules import their tables
from shared modules. The checks don't follow imports made through
``importlib`` or ``__import__``, tables or engines passed around without a
type annotation, or a Session's ``bind`` attribute.
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
# Core's table declarations, the lightweight table() and column() included.
# MetaData may be built only in a declarative base module, as the metadata of
# the library's DeclarativeBase.
TABLE_CONSTRUCTORS = frozenset({"Column", "MetaData", "Table", "column", "table"})
TABLE_FACTORIES = frozenset({"Table", "table"})
METADATA_CONSTRUCTOR = "MetaData"
DECLARATIVE_BASE_MODULE_NAME = "orm_base.py"
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
CONNECTION_METHODS = frozenset({"connect", "connection", "get_bind", "raw_connection"})
STATEMENT_FUNCTIONS = frozenset({"delete", "insert", "select", "update"})
# Statement methods that take a table to read from or join.
QUERY_METHODS = frozenset({"join", "join_from", "outerjoin", "select_from"})
# Attributes that reach a Core table from a mapped class, its mapper or a
# MetaData.
TABLE_ATTRIBUTES = frozenset(
    {"__table__", "local_table", "persist_selectable", "sorted_tables"}
)
METADATA_TABLES_ATTRIBUTE = "tables"
METADATA_SUFFIX = "metadata"
# A Core table's column collections.
COLUMN_ATTRIBUTES = frozenset({"c", "columns"})
RAW_SQL_FUNCTIONS = frozenset({"literal_column", "text"})
RAW_SQL_METHODS = frozenset({"cursor", "exec_driver_sql"})
# A model's schema details that SQLAlchemy accepts only as SQL text: a server
# default and a partial index's condition.
SCHEMA_DETAIL_KEYWORDS = frozenset(
    {"postgresql_where", "server_default", "server_onupdate", "sqlite_where"}
)

CORE_TABLE_DECLARATION = "Core table declaration"
CORE_TABLE_ACCESS = "Core table access"
CORE_CONNECTION = "Core engine or connection"
STATEMENT_ON_CORE_TABLE = "statement on a Core table"
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


def _bound_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    return []


def _core_table_names(trees: Iterable[ast.Module]) -> frozenset[str]:
    """Return the names bound to a Core table in any of the modules.

    A name bound to ``Table(...)`` or ``table(...)``, or annotated as
    ``Table``, is a table, and so is a name a module imports one under.
    """
    trees = list(trees)
    names = set().union(*(table_names(tree) for tree in trees))
    for tree in trees:
        imported = _sqlalchemy_imports(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(node.value, ast.Call)
                and _sqlalchemy_name(node.value.func, imported) in TABLE_FACTORIES
            ):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                names.update(
                    name for target in targets for name in _bound_names(target)
                )
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(
                    alias.asname
                    for alias in node.names
                    if alias.asname and alias.name in names
                )
    return frozenset(names)


def _root_name(node: ast.expr) -> str:
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript, ast.Starred)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else ""


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
    core_tables: frozenset[str]
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


def _reaches_core_table(node: ast.Attribute, context: _ModuleContext) -> bool:
    if node.attr in TABLE_ATTRIBUTES:
        return True
    if node.attr == METADATA_TABLES_ATTRIBUTE:
        receiver = node.value
        receiver_name = (
            receiver.attr
            if isinstance(receiver, ast.Attribute)
            else _root_name(receiver)
        )
        return receiver_name.lower().endswith(METADATA_SUFFIX)
    return (
        node.attr in COLUMN_ATTRIBUTES and _root_name(node.value) in context.core_tables
    )


def _attribute_findings(
    node: ast.Attribute, context: _ModuleContext
) -> Iterator[tuple[str, str]]:
    if _reaches_core_table(node, context):
        yield CORE_TABLE_ACCESS, ast.unparse(node)
    elif not context.is_session_factory and (
        _sqlalchemy_name(node, context.imported) in CONNECTION_NAMES
    ):
        yield CORE_CONNECTION, str(_qualified_name(node, context.imported))


def _runs_raw_sql(node: ast.Call, function: str, context: _ModuleContext) -> bool:
    if function in RAW_SQL_FUNCTIONS:
        return id(node) not in context.schema_details
    return isinstance(node.func, ast.Attribute) and node.func.attr in RAW_SQL_METHODS


def _builds_statement(node: ast.Call, function: str) -> bool:
    if function in STATEMENT_FUNCTIONS:
        return True
    return isinstance(node.func, ast.Attribute) and node.func.attr in QUERY_METHODS


def _call_findings(
    node: ast.Call, context: _ModuleContext
) -> Iterator[tuple[str, str]]:
    function = _sqlalchemy_name(node.func, context.imported)
    if function in TABLE_CONSTRUCTORS and not (
        function == METADATA_CONSTRUCTOR and context.is_declarative_base
    ):
        yield CORE_TABLE_DECLARATION, _call_summary(node)
    elif _builds_statement(node, function) and any(
        _root_name(argument) in context.core_tables for argument in node.args
    ):
        yield STATEMENT_ON_CORE_TABLE, _call_summary(node)
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

    ``sources`` maps a repository-relative file name to its source. Core
    tables bound in one file are recognised in all of them.
    """
    trees = {file: ast.parse(source, filename=file) for file, source in sources.items()}
    core_tables = _core_table_names(trees.values())
    findings: list[Finding] = []
    for file, tree in trees.items():
        context = _ModuleContext(
            file=file,
            imported=_sqlalchemy_imports(tree),
            core_tables=core_tables,
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
    from sqlalchemy.orm import DeclarativeBase, DeclarativeBaseNoMeta

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
                and issubclass(value, (DeclarativeBase, DeclarativeBaseNoMeta))
                and not value.__module__.startswith(SQLALCHEMY_MODULE)
            ):
                mapped.update(
                    id(mapper.local_table) for mapper in value.registry.mappers
                )
                tables = list(value.registry.metadata.tables.values())
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
    """Import the packages and return each table they hold that no class maps.

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

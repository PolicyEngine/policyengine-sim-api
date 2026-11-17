"""Checks behind sim.persistence.orm-only in rules/registry.toml.

Database code declares tables only as SQLAlchemy ORM mapped classes and runs
every statement through an ORM Session against them.
``orm_access_findings`` reads source, so the rule test runs it on the
repository and the proof test runs it on the fixtures under
``rules/fixtures/sim.persistence.orm-only/``. ``unmapped_table_findings``
reports every table that given modules hold and no mapped class maps, and
``unmapped_tables_in_packages`` imports whole packages for it, which finds
tables built in ways the source checks can't see.

The source checks look for Core rather than for the ORM, so any statement on
mapped classes passes. They resolve names through each module's own imports:
``from sqlalchemy import text`` and ``import sqlalchemy as sa`` both count,
and a ``text``, ``Table`` or ``update`` from another package doesn't. A Core
table is, within one module:

- a name the module binds to ``Table(...)`` or ``table(...)``;
- a name it imports, under any name, from a scanned module that binds or
  imports a Core table under it at module level, and an attribute such as
  ``tables.household_jobs`` of such a module;
- a parameter or variable annotated as SQLAlchemy's ``Table``;
- a name it assigns from an expression rooted at a Core table, such as
  ``previous = household_jobs.alias()``.

Every reference to a Core table is a finding, so its statements, methods,
column collections and joins all count. The checks don't follow imports made
through ``importlib`` or ``__import__``, tables or engines passed around
without a type annotation, tables held in a container such as a dict, or a
Session's ``bind`` attribute.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from types import ModuleType

SQLALCHEMY_MODULE = "sqlalchemy"
SQLMODEL_MODULE = "sqlmodel"
SOURCE_DIRECTORY = "src"
PACKAGE_MODULE = "__init__"
# Core's table declarations, the lightweight table() and column() included.
# MetaData may be built only in a declarative base module, as the metadata of
# the library's DeclarativeBase.
TABLE_CONSTRUCTORS = frozenset({"Column", "MetaData", "Table", "column", "table"})
TABLE_FACTORIES = frozenset({"Table", "table"})
TABLE_TYPE = "Table"
METADATA_CONSTRUCTOR = "MetaData"
DECLARATIVE_BASE_MODULE_NAME = "orm_base.py"
# The one module per library that may create and hold an engine, to hand out
# a sessionmaker. It may not open a Connection either.
SESSION_FACTORY_PATTERN = "libs/*/src/*/engine.py"
ENGINE_NAMES = frozenset(
    {
        "AsyncEngine",
        "Engine",
        "create_async_engine",
        "create_engine",
        "engine_from_config",
    }
)
CONNECTION_NAMES = frozenset({"AsyncConnection", "Connection"})
ENGINE_FACTORIES = frozenset({"create_async_engine", "create_engine"})
ENGINE_TYPES = frozenset({"AsyncEngine", "Engine"})
ENGINE_NAME_PART = "engine"
# Calls that open a Connection: on anything, and begin() on an engine.
CONNECTION_METHODS = frozenset({"connect", "connection", "get_bind", "raw_connection"})
ENGINE_TRANSACTION_METHOD = "begin"
# Attributes that reach a Core table from a mapped class, its mapper or a
# MetaData, and the ones that do so only on a mapper, inspect() or a MetaData.
TABLE_ATTRIBUTES = frozenset(
    {"__table__", "local_table", "persist_selectable", "sorted_tables"}
)
MAPPER_TABLE_ATTRIBUTES = frozenset({"selectable", "tables"})
MAPPER_ATTRIBUTE = "__mapper__"
INSPECT_FUNCTION = "inspect"
METADATA_SUFFIX = "metadata"
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


def _module_name(file: str) -> str:
    """Return the dotted module name of a file, counted from its ``src``."""
    parts = PurePosixPath(file).with_suffix("").parts
    if SOURCE_DIRECTORY in parts:
        parts = parts[parts.index(SOURCE_DIRECTORY) + 1 :]
    if parts and parts[-1] == PACKAGE_MODULE:
        parts = parts[:-1]
    return ".".join(parts)


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


def _annotation_names(
    annotation: ast.expr | None, imported: Mapping[str, str]
) -> set[str]:
    """Return the sqlalchemy names an annotation refers to, unions included."""
    if annotation is None:
        return set()
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return set()
    return {
        name
        for node in ast.walk(annotation)
        if isinstance(node, (ast.Name, ast.Attribute))
        and (name := _sqlalchemy_name(node, imported))
    }


def _bound_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    return []


def _assignments(tree: ast.AST) -> Iterator[tuple[list[str], ast.expr]]:
    """Yield the names each assignment binds, with the value it binds them to."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [name for target in node.targets for name in _bound_names(target)]
            yield names, node.value
        elif (isinstance(node, ast.AnnAssign) and node.value is not None) or isinstance(
            node, ast.NamedExpr
        ):
            yield _bound_names(node.target), node.value


def _root_name(node: ast.expr) -> str:
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript, ast.Starred)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else ""


def _last_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _last_name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            names.update(
                name for target in statement.targets for name in _bound_names(target)
            )
        elif isinstance(statement, ast.AnnAssign):
            names.update(_bound_names(statement.target))
        elif isinstance(statement, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in statement.names)
    return names


@dataclass
class _Module:
    """One scanned file and what it binds."""

    file: str
    tree: ast.Module
    imported: dict[str, str]
    core_tables: set[str] = field(default_factory=set)
    exports: set[str] = field(default_factory=set)
    module_aliases: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return _module_name(self.file)

    @property
    def package(self) -> str:
        if PurePosixPath(self.file).stem == PACKAGE_MODULE:
            return self.name
        return self.name.rpartition(".")[0]

    def resolve(self, node: ast.ImportFrom) -> str | None:
        """Return the module an ``import from`` names, relative ones included."""
        if not node.level:
            return node.module
        base = self.package.split(".") if self.package else []
        if node.level > 1:
            base = base[: len(base) - (node.level - 1)]
        return ".".join([*base, *([node.module] if node.module else [])])

    @property
    def is_session_factory(self) -> bool:
        return PurePosixPath(self.file).match(SESSION_FACTORY_PATTERN)

    @property
    def is_declarative_base(self) -> bool:
        return PurePosixPath(self.file).name == DECLARATIVE_BASE_MODULE_NAME


def _imported_tables(module: _Module, modules: Mapping[str, _Module]) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(module.tree):
        if isinstance(node, ast.ImportFrom):
            source = modules.get(module.resolve(node) or "")
            if source is not None:
                names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in source.exports
                )
    return names


def _annotated_as(module: _Module, types: frozenset[str]) -> set[str]:
    """Return the parameters and variables annotated with one of ``types``."""
    names: set[str] = set()
    for node in ast.walk(module.tree):
        if isinstance(node, ast.arg):
            if types & _annotation_names(node.annotation, module.imported):
                names.add(node.arg)
        elif isinstance(node, ast.AnnAssign) and types & _annotation_names(
            node.annotation, module.imported
        ):
            names.update(_bound_names(node.target))
    return names


def _bound_to_calls(module: _Module, functions: frozenset[str]) -> set[str]:
    """Return the names the module binds to a call of one of ``functions``."""
    return {
        name
        for targets, value in _assignments(module.tree)
        if isinstance(value, ast.Call)
        and _sqlalchemy_name(value.func, module.imported) in functions
        for name in targets
    }


def _core_tables(module: _Module, modules: Mapping[str, _Module]) -> set[str]:
    """Return the names that hold a Core table within the module."""
    names = (
        _imported_tables(module, modules)
        | _annotated_as(module, frozenset({TABLE_TYPE}))
        | _bound_to_calls(module, TABLE_FACTORIES)
    )
    assignments = list(_assignments(module.tree))
    grew = True
    while grew:
        derived = {
            name
            for targets, value in assignments
            if _root_name(value) in names
            for name in targets
        }
        grew = not derived <= names
        names |= derived
    return names


def _module_aliases(module: _Module, modules: Mapping[str, _Module]) -> dict[str, str]:
    """Map each name the module binds to another scanned module to that module."""
    aliases: dict[str, str] = {}
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name in modules:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom):
            parent = module.resolve(node)
            for alias in node.names:
                dotted = f"{parent}.{alias.name}" if parent else alias.name
                if dotted in modules:
                    aliases[alias.asname or alias.name] = dotted
    return aliases


def _index_modules(sources: Mapping[str, str]) -> dict[str, _Module]:
    modules: dict[str, _Module] = {}
    for file, source in sources.items():
        tree = ast.parse(source, filename=file)
        module = _Module(file=file, tree=tree, imported=_sqlalchemy_imports(tree))
        modules[module.name] = module
    # A module exports the Core tables it binds or imports at module level;
    # imports chain through packages, so repeat until no export is added.
    grew = True
    while grew:
        grew = False
        for module in modules.values():
            module.core_tables = _core_tables(module, modules)
            exports = module.core_tables & _module_level_names(module.tree)
            if exports != module.exports:
                module.exports = exports
                grew = True
    for module in modules.values():
        module.module_aliases = _module_aliases(module, modules)
    return modules


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
    module: _Module
    exports: Mapping[str, set[str]]
    metadata_names: set[str]
    engine_names: set[str]
    schema_details: set[int]


def _forbidden_connection_name(name: str, context: _ModuleContext) -> bool:
    if name in CONNECTION_NAMES:
        return True
    return name in ENGINE_NAMES and not context.module.is_session_factory


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
    elif _matches_module(node.module, SQLALCHEMY_MODULE):
        for alias in node.names:
            if _forbidden_connection_name(alias.name, context):
                yield CORE_CONNECTION, f"{node.module}.{alias.name}"


def _reaches_mapped_table(node: ast.Attribute, context: _ModuleContext) -> bool:
    if node.attr in TABLE_ATTRIBUTES:
        return True
    if node.attr not in MAPPER_TABLE_ATTRIBUTES:
        return False
    receiver = node.value
    if isinstance(receiver, ast.Call):
        function = _sqlalchemy_name(receiver.func, context.module.imported)
        return function == INSPECT_FUNCTION
    receiver_name = _last_name(receiver)
    return (
        receiver_name == MAPPER_ATTRIBUTE
        or receiver_name in context.metadata_names
        or receiver_name.lower().endswith(METADATA_SUFFIX)
    )


def _names_exported_table(node: ast.Attribute, context: _ModuleContext) -> bool:
    if not isinstance(node.value, ast.Name):
        return False
    aliased_module = context.module.module_aliases.get(node.value.id)
    return aliased_module is not None and node.attr in context.exports.get(
        aliased_module, set()
    )


def _attribute_findings(
    node: ast.Attribute, context: _ModuleContext
) -> Iterator[tuple[str, str]]:
    if _names_exported_table(node, context) or _reaches_mapped_table(node, context):
        yield CORE_TABLE_ACCESS, ast.unparse(node)
    elif _forbidden_connection_name(
        _sqlalchemy_name(node, context.module.imported), context
    ):
        yield CORE_CONNECTION, str(_qualified_name(node, context.module.imported))


def _runs_raw_sql(node: ast.Call, function: str, context: _ModuleContext) -> bool:
    if function in RAW_SQL_FUNCTIONS:
        return id(node) not in context.schema_details
    return isinstance(node.func, ast.Attribute) and node.func.attr in RAW_SQL_METHODS


def _opens_connection(node: ast.Call, context: _ModuleContext) -> bool:
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr in CONNECTION_METHODS:
        return True
    if node.func.attr != ENGINE_TRANSACTION_METHOD:
        return False
    receiver = _last_name(node.func.value)
    return receiver in context.engine_names or ENGINE_NAME_PART in receiver.lower()


def _call_findings(
    node: ast.Call, context: _ModuleContext
) -> Iterator[tuple[str, str]]:
    function = _sqlalchemy_name(node.func, context.module.imported)
    if function in TABLE_CONSTRUCTORS and not (
        function == METADATA_CONSTRUCTOR and context.module.is_declarative_base
    ):
        yield CORE_TABLE_DECLARATION, _call_summary(node)
    elif _runs_raw_sql(node, function, context):
        yield RAW_SQL, _call_summary(node)
    elif _opens_connection(node, context):
        yield CORE_CONNECTION, _call_summary(node)


def _node_findings(node: ast.AST, context: _ModuleContext) -> list[tuple[str, str]]:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return list(_import_findings(node, context))
    if isinstance(node, ast.Name):
        loads_table = (
            isinstance(node.ctx, ast.Load) and node.id in context.module.core_tables
        )
        return [(CORE_TABLE_ACCESS, node.id)] if loads_table else []
    if isinstance(node, ast.Attribute):
        return list(_attribute_findings(node, context))
    if isinstance(node, ast.Call):
        return list(_call_findings(node, context))
    return []


def orm_access_findings(sources: Mapping[str, str]) -> list[Finding]:
    """Return every place the sources reach the database without the ORM.

    ``sources`` maps a repository-relative file name to its source. Each file's
    module name is its path from its ``src`` directory, so modules import each
    other's Core tables by those names.
    """
    modules = _index_modules(sources)
    exports = {name: module.exports for name, module in modules.items()}
    findings: list[Finding] = []
    for module in modules.values():
        context = _ModuleContext(
            module=module,
            exports=exports,
            metadata_names=_bound_to_calls(module, frozenset({METADATA_CONSTRUCTOR})),
            engine_names=_bound_to_calls(module, ENGINE_FACTORIES)
            | _annotated_as(module, ENGINE_TYPES),
            schema_details=_schema_detail_calls(module.tree),
        )
        findings.extend(
            Finding(module.file, getattr(node, "lineno", 0), check, source)
            for node in ast.walk(module.tree)
            for check, source in _node_findings(node, context)
        )
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

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
mapped classes passes. Each file is checked on its own, through its own imports:
``from sqlalchemy import text`` and ``import sqlalchemy as sa`` both count, and
a ``text``, ``Table`` or ``update`` from another package doesn't. A Core table is
reported where it starts: where it is declared, where it is read from a mapped
class, mapper or MetaData, and where an annotation names ``Table`` or
``TableClause``, apart from a class's bare ``__table__`` annotation in a
declarative base module. Its later uses aren't reported, because the rule has no
baseline and the first finding already fails it.

A public function of an engine module may not return an Engine or Connection,
and only an engine module may read ``factory.kw["bind"]``. The rules that go by a
method or attribute name alone apply only in modules that import sqlalchemy: any
``.table`` read, ``kw["bind"]``, ``.execute()`` on anything not named as a
session, ``.scalar()``, ``.scalars()``, ``.stream()`` and ``.stream_scalars()`` on
a name such as ``connection``, and the ``.connect()``, ``.connection()``,
``.raw_connection()``, ``.get_bind()``, ``.begin()``, ``.cursor()`` and
``.exec_driver_sql()`` calls.

The checks guard against ordinary Core use, not against deliberate evasion. They
don't see imports made through ``importlib`` or ``__import__``, engines or
connections passed around without an ``Engine`` or ``Connection`` annotation, so
a connection held under a name such as ``session`` isn't reported, a statement
method passed without being called, a function that returns an engine with no
return annotation, the engine an ``Inspector`` holds in ``bind`` or ``engine``, a
quoted name inside an annotation such as ``Optional["Table"]``, ``Table`` under
another local name such as ``Jobs = Table``, names from ``from sqlalchemy import
*``, a ``.table`` read in a module that doesn't import sqlalchemy, or a Session's
``bind`` attribute. ``.tables`` of
a MetaData isn't reported when the name holding it doesn't end in ``metadata`` and
wasn't assigned ``MetaData(...)``.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from types import ModuleType

SQLALCHEMY_MODULE = "sqlalchemy"
SQLMODEL_MODULE = "sqlmodel"
# Core's table declarations, the lightweight table() and column() included.
# MetaData may be built only in a declarative base module, as the metadata of
# one of the library's DeclarativeBase classes.
TABLE_CONSTRUCTORS = frozenset(
    {"Column", "ColumnClause", "MetaData", "Table", "TableClause", "column", "table"}
)
TABLE_TYPES = frozenset({"Table", "TableClause"})
# A declarative base's classes may annotate what each of them maps.
MAPPED_TABLE_ATTRIBUTE = "__table__"
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
# A name such as engine or simulation_engine, but not policyengine.
ENGINE_NAME_PATTERN = re.compile(r"(?:^|_)engine$", re.IGNORECASE)
# Calls that open a Connection: on anything, and begin() on an engine.
CONNECTION_METHODS = frozenset({"connect", "connection", "get_bind", "raw_connection"})
ENGINE_TRANSACTION_METHOD = "begin"
# execute runs only on a name such as session or store_session, which finds a
# connection passed around without a Connection annotation. A Result has scalar
# and scalars too, so the others are reported only on a name such as connection.
EXECUTE_METHOD = "execute"
SESSION_NAME_PATTERN = re.compile(r"(?:^|_)session$", re.IGNORECASE)
CONNECTION_STATEMENT_METHODS = frozenset(
    {"scalar", "scalars", "stream", "stream_scalars"}
)
CONNECTION_NAME_PATTERN = re.compile(r"(?:^|_)(?:connection|conn)$", re.IGNORECASE)
# What an engine module keeps to itself: no public function returns one of
# these, and only it reads the engine out of a sessionmaker's keywords.
HELD_DATABASE_TYPES = ENGINE_TYPES | CONNECTION_NAMES
SESSION_FACTORY_KEYWORDS = "kw"
SESSION_FACTORY_BIND_KEY = "bind"
# Attributes that reach a Core table from a mapped class, its mapper or a
# MetaData, and the ones that do so only on a mapper, inspect() or a MetaData.
TABLE_ATTRIBUTES = frozenset(
    {"__table__", "local_table", "persist_selectable", "sorted_tables"}
)
MAPPER_TABLE_ATTRIBUTES = frozenset({"selectable", "tables"})
MAPPER_FUNCTIONS = frozenset({"class_mapper", "inspect", "object_mapper"})
MAPPER_NAMES = frozenset({"__mapper__", "mapper"})
# A column's or mapped attribute's table, as in HouseholdJob.id.table: read in a
# module that imports sqlalchemy, any .table is reported.
COLUMN_TABLE_ATTRIBUTE = "table"
METADATA_SUFFIX = "metadata"
RAW_SQL_FUNCTIONS = frozenset({"TextClause", "literal_column", "text"})
RAW_SQL_METHODS = frozenset({"cursor", "exec_driver_sql"})
# A model's schema details given as SQL text: a server default and a partial
# index's condition, by keyword, and a check constraint's condition.
SCHEMA_DETAIL_KEYWORDS = frozenset(
    {"postgresql_where", "server_default", "server_onupdate", "sqlite_where"}
)
CHECK_CONSTRAINT = "CheckConstraint"

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

    def __str__(self) -> str:
        """Describe the finding as ``file:line: check: source``."""
        return f"{self.file}:{self.line}: {self.check}: {self.source}"


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


def _annotation_names(
    annotation: ast.expr | None, imported: Mapping[str, str]
) -> set[str]:
    """Return the sqlalchemy names an annotation refers to, unions included.

    A wholly quoted annotation is parsed. Strings inside one aren't, because
    they may be anything, such as ``Literal`` values or ``Annotated`` metadata.
    """
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


def _bound_targets(target: ast.expr) -> list[str]:
    """Return the names and attributes an assignment target binds."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_targets(element)]
    return []


def _last_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _last_name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


@dataclass
class _Module:
    """One scanned file and the names it imports from sqlalchemy."""

    file: str
    tree: ast.Module
    imported: dict[str, str]

    @property
    def uses_sqlalchemy(self) -> bool:
        """Whether the module imports anything from sqlalchemy."""
        return bool(self.imported)

    @property
    def is_session_factory(self) -> bool:
        return PurePosixPath(self.file).match(SESSION_FACTORY_PATTERN)

    @property
    def is_declarative_base(self) -> bool:
        return PurePosixPath(self.file).name == DECLARATIVE_BASE_MODULE_NAME


def _annotated_as(module: _Module, types: frozenset[str]) -> set[str]:
    """Return the names and attributes annotated with one of ``types``."""
    names: set[str] = set()
    for node in ast.walk(module.tree):
        if isinstance(node, ast.arg):
            if types & _annotation_names(node.annotation, module.imported):
                names.add(node.arg)
        elif isinstance(node, ast.AnnAssign) and types & _annotation_names(
            node.annotation, module.imported
        ):
            names.update(_bound_targets(node.target))
    return names


def _bound_to_calls(module: _Module, functions: frozenset[str]) -> set[str]:
    """Return the names and attributes bound to a call of one of ``functions``."""
    return {
        name
        for target, value in _assignment_nodes(module.tree)
        if isinstance(value, ast.Call)
        and _sqlalchemy_name(value.func, module.imported) in functions
        for name in _bound_targets(target)
    }


def _paired(target: ast.expr, value: ast.expr) -> Iterator[tuple[ast.expr, ast.expr]]:
    """Pair each part of a tuple target with its value, nested tuples included."""
    if (
        isinstance(target, (ast.Tuple, ast.List))
        and isinstance(value, (ast.Tuple, ast.List))
        and len(target.elts) == len(value.elts)
    ):
        for element, element_value in zip(target.elts, value.elts, strict=True):
            yield from _paired(element, element_value)
    else:
        yield target, value


def _assignment_nodes(tree: ast.AST) -> Iterator[tuple[ast.expr, ast.expr]]:
    """Yield each assignment's target and value, pairing tuples.

    ``a, b = x, y`` yields ``a`` with ``x`` and ``b`` with ``y``.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield from _paired(target, node.value)
        elif (isinstance(node, ast.AnnAssign) and node.value is not None) or isinstance(
            node, ast.NamedExpr
        ):
            yield node.target, node.value


def _bound_to_mappers(module: _Module) -> set[str]:
    """Return the names and attributes assigned a mapper, as in ``m = X.__mapper__``."""
    return {
        name
        for target, value in _assignment_nodes(module.tree)
        if isinstance(value, ast.Attribute) and value.attr in MAPPER_NAMES
        for name in _bound_targets(target)
    }


def _call_summary(call: ast.Call) -> str:
    parts = [ast.unparse(call.args[0])] if call.args else []
    if len(call.args) > 1 or call.keywords:
        parts.append("...")
    return f"{ast.unparse(call.func)}({', '.join(parts)})"


def _schema_detail_calls(tree: ast.Module, imported: Mapping[str, str]) -> set[int]:
    """Return the calls given as a default or as an index's or check's condition."""
    details = {
        id(keyword.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg in SCHEMA_DETAIL_KEYWORDS
    }
    details.update(
        id(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and node.args
        and _sqlalchemy_name(node.func, imported) == CHECK_CONSTRAINT
    )
    return details


@dataclass(frozen=True)
class _ModuleContext:
    module: _Module
    metadata_names: set[str]
    mapper_names: set[str]
    engine_names: set[str]
    schema_details: set[int]
    called: set[int]
    mapped_table_annotations: set[int]


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
    receiver = node.value
    if node.attr == COLUMN_TABLE_ATTRIBUTE:
        # A column's table is read, never called: sa.table(...) is a function.
        return (
            context.module.uses_sqlalchemy
            and isinstance(node.ctx, ast.Load)
            and id(node) not in context.called
        )
    if node.attr not in MAPPER_TABLE_ATTRIBUTES:
        return False
    if isinstance(receiver, ast.Call):
        function = _sqlalchemy_name(receiver.func, context.module.imported)
        return function in MAPPER_FUNCTIONS
    receiver_name = _last_name(receiver)
    return (
        receiver_name in MAPPER_NAMES
        or receiver_name in context.mapper_names
        or receiver_name in context.metadata_names
        or receiver_name.lower().endswith(METADATA_SUFFIX)
    )


def _attribute_findings(
    node: ast.Attribute, context: _ModuleContext
) -> Iterator[tuple[str, str]]:
    if _reaches_mapped_table(node, context):
        yield CORE_TABLE_ACCESS, ast.unparse(node)
    elif _forbidden_connection_name(
        _sqlalchemy_name(node, context.module.imported), context
    ):
        yield CORE_CONNECTION, str(_qualified_name(node, context.module.imported))


def _runs_raw_sql(node: ast.Call, function: str, context: _ModuleContext) -> bool:
    if function in RAW_SQL_FUNCTIONS:
        return id(node) not in context.schema_details
    return (
        context.module.uses_sqlalchemy
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in RAW_SQL_METHODS
    )


def _opens_connection(node: ast.Call, context: _ModuleContext) -> bool:
    if not context.module.uses_sqlalchemy or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr in CONNECTION_METHODS:
        return True
    if node.func.attr != ENGINE_TRANSACTION_METHOD:
        return False
    receiver = _last_name(node.func.value)
    return receiver in context.engine_names or bool(
        ENGINE_NAME_PATTERN.search(receiver)
    )


def _runs_outside_a_session(node: ast.Call, context: _ModuleContext) -> bool:
    """Whether a statement runs on something other than a session."""
    if not context.module.uses_sqlalchemy or not isinstance(node.func, ast.Attribute):
        return False
    receiver = _last_name(node.func.value)
    if node.func.attr == EXECUTE_METHOD:
        return not SESSION_NAME_PATTERN.search(receiver)
    return node.func.attr in CONNECTION_STATEMENT_METHODS and bool(
        CONNECTION_NAME_PATTERN.search(receiver)
    )


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
    elif _runs_outside_a_session(node, context):
        yield CORE_CONNECTION, f"{ast.unparse(node.func)}(...)"


def _annotation_text(annotation: ast.expr) -> str:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value
    return ast.unparse(annotation)


def _table_annotation_findings(
    label: str, annotation: ast.expr | None, context: _ModuleContext
) -> list[tuple[str, str]]:
    """Report an annotation naming a Core table type, as in ``jobs: Table``."""
    names = _annotation_names(annotation, context.module.imported)
    if annotation is None or not names & TABLE_TYPES:
        return []
    return [(CORE_TABLE_ACCESS, f"{label}{_annotation_text(annotation)}")]


def _class_level_statements(node: ast.ClassDef) -> Iterator[ast.stmt]:
    """Yield a class's own statements, including those under an ``if``."""
    pending: list[ast.stmt] = list(node.body)
    while pending:
        statement = pending.pop()
        yield statement
        if isinstance(statement, ast.If):
            pending.extend([*statement.body, *statement.orelse])


def _mapped_table_annotations(module: _Module) -> set[int]:
    """Return each class's bare ``__table__`` annotation in a declarative base."""
    if not module.is_declarative_base:
        return set()
    return {
        id(statement)
        for node in ast.walk(module.tree)
        if isinstance(node, ast.ClassDef)
        for statement in _class_level_statements(node)
        if isinstance(statement, ast.AnnAssign)
        and statement.value is None
        and isinstance(statement.target, ast.Name)
        and statement.target.id == MAPPED_TABLE_ATTRIBUTE
    }


def _annotated_assignment_findings(
    node: ast.AnnAssign, context: _ModuleContext
) -> list[tuple[str, str]]:
    if id(node) in context.mapped_table_annotations:
        return []
    return _table_annotation_findings(
        f"{ast.unparse(node.target)}: ", node.annotation, context
    )


def _function_findings(
    node: ast.FunctionDef | ast.AsyncFunctionDef, context: _ModuleContext
) -> list[tuple[str, str]]:
    """Report a function returning a Core table, or an engine module's engine."""
    findings = _table_annotation_findings(f"{node.name}() -> ", node.returns, context)
    if not context.module.is_session_factory or node.name.startswith("_"):
        return findings
    returned = _annotation_names(node.returns, context.module.imported)
    return findings + [
        (CORE_CONNECTION, f"{node.name}() -> {type_name}")
        for type_name in sorted(returned & HELD_DATABASE_TYPES)
    ]


def _reads_session_factory_engine(node: ast.Subscript, context: _ModuleContext) -> bool:
    """Whether ``node`` is ``factory.kw["bind"]`` outside an engine module."""
    return (
        context.module.uses_sqlalchemy
        and not context.module.is_session_factory
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == SESSION_FACTORY_KEYWORDS
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == SESSION_FACTORY_BIND_KEY
    )


def _subscript_findings(
    node: ast.Subscript, context: _ModuleContext
) -> list[tuple[str, str]]:
    if _reads_session_factory_engine(node, context):
        return [(CORE_CONNECTION, ast.unparse(node))]
    return []


def _node_findings(node: ast.AST, context: _ModuleContext) -> list[tuple[str, str]]:
    findings: Iterable[tuple[str, str]] = ()
    match node:
        case ast.Import() | ast.ImportFrom():
            findings = _import_findings(node, context)
        case ast.Attribute():
            findings = _attribute_findings(node, context)
        case ast.Call():
            findings = _call_findings(node, context)
        case ast.FunctionDef() | ast.AsyncFunctionDef():
            findings = _function_findings(node, context)
        case ast.arg():
            findings = _table_annotation_findings(
                f"{node.arg}: ", node.annotation, context
            )
        case ast.AnnAssign():
            findings = _annotated_assignment_findings(node, context)
        case ast.Subscript():
            findings = _subscript_findings(node, context)
    return list(findings)


def _module_context(module: _Module) -> _ModuleContext:
    return _ModuleContext(
        module=module,
        metadata_names=_bound_to_calls(module, frozenset({METADATA_CONSTRUCTOR})),
        mapper_names=_bound_to_calls(module, MAPPER_FUNCTIONS)
        | _bound_to_mappers(module),
        engine_names=_bound_to_calls(module, ENGINE_FACTORIES)
        | _annotated_as(module, ENGINE_TYPES),
        schema_details=_schema_detail_calls(module.tree, module.imported),
        called={
            id(node.func)
            for node in ast.walk(module.tree)
            if isinstance(node, ast.Call)
        },
        mapped_table_annotations=_mapped_table_annotations(module),
    )


def orm_access_findings(sources: Mapping[str, str]) -> list[Finding]:
    """Return every place the sources reach the database without the ORM.

    ``sources`` maps a repository-relative file name to its source.
    """
    findings: list[Finding] = []
    for file, source in sorted(sources.items()):
        tree = ast.parse(source, filename=file)
        module = _Module(file=file, tree=tree, imported=_sqlalchemy_imports(tree))
        context = _module_context(module)
        findings.extend(
            Finding(file, getattr(node, "lineno", 0), check, finding_source)
            for node in ast.walk(tree)
            for check, finding_source in _node_findings(node, context)
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

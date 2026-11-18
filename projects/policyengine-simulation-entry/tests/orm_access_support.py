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

- a name the module binds to ``Table(...)``, ``table(...)`` or
  ``TableClause(...)``;
- a name it imports, under any name or through ``*``, from a scanned module
  that binds or imports a Core table under it at module level, and an
  attribute such as ``tables.household_jobs`` or
  ``example.tables.household_jobs`` of such a module;
- a parameter, variable or attribute annotated as SQLAlchemy's ``Table``;
- a name or attribute it assigns from an expression rooted at a Core table,
  such as ``previous = household_jobs.alias()`` or ``self.jobs =
  tables.household_jobs``, or from one that reads a mapped class's table, such
  as ``household_jobs = HouseholdJob.__table__``, pairing the names and values
  of a tuple assignment;
- a class attribute it declares as a table, such as a dataclass field
  ``table: Table``, wherever the module reads it.

Every reference to a Core table is a finding, so its statements, methods,
column collections and joins all count. Each file's module name is its path
below ``src``; two files with one module name are refused, since imports
resolve by name. Eight rules go by a method or attribute name alone, so they
apply only in modules that import sqlalchemy: any ``.table`` read,
and the ``.connect()``, ``.connection()``, ``.raw_connection()``,
``.get_bind()``, ``.begin()``, ``.cursor()`` and ``.exec_driver_sql()`` calls.
The checks guard against ordinary Core use, not against deliberate evasion.
They don't follow imports made through ``importlib`` or ``__import__``, tables
or engines passed around without a ``Table`` or ``Engine`` annotation, a
``.table`` read in a module that doesn't import sqlalchemy, tables held in a
container such as a dict, a class or instance attribute read in another
module (unless it is a ``.table`` read, or its name matches one of the reading
module's own table attributes), or a Session's ``bind`` attribute, and a name
derived from a Core table counts as one throughout its module.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
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
TABLE_CONSTRUCTORS = frozenset(
    {"Column", "ColumnClause", "MetaData", "Table", "TableClause", "column", "table"}
)
TABLE_FACTORIES = frozenset({"Table", "TableClause", "table"})
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
# A name such as engine or simulation_engine, but not policyengine.
ENGINE_NAME_PATTERN = re.compile(r"(?:^|_)engine$", re.IGNORECASE)
STAR_IMPORT = "*"
# Calls that open a Connection: on anything, and begin() on an engine.
CONNECTION_METHODS = frozenset({"connect", "connection", "get_bind", "raw_connection"})
ENGINE_TRANSACTION_METHOD = "begin"
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
    """Return the plain names an assignment target binds."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    return []


def _bound_attributes(target: ast.expr) -> list[str]:
    """Return the attribute names an assignment target binds, as in ``self.x``."""
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_attributes(element)]
    return []


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
    module_aliases: dict[str, str] = field(default_factory=dict)
    packages: set[str] = field(default_factory=set)
    core_tables: set[str] = field(default_factory=set)
    core_attributes: set[str] = field(default_factory=set)
    star_names: set[str] = field(default_factory=set)
    exports: set[str] = field(default_factory=set)

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
    def uses_sqlalchemy(self) -> bool:
        """Whether the module imports anything from sqlalchemy."""
        return bool(self.imported)

    @property
    def is_session_factory(self) -> bool:
        return PurePosixPath(self.file).match(SESSION_FACTORY_PATTERN)

    @property
    def is_declarative_base(self) -> bool:
        return PurePosixPath(self.file).name == DECLARATIVE_BASE_MODULE_NAME


def _module_aliases(module: _Module, packages: set[str]) -> dict[str, str]:
    """Map each name the module binds to a scanned module or package to it.

    ``import a.b`` binds ``a``, and ``a.b.household_jobs`` then resolves
    through the dotted path.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                dotted = alias.name if alias.asname else bound
                if dotted in packages:
                    aliases[bound] = dotted
        elif isinstance(node, ast.ImportFrom):
            parent = module.resolve(node)
            for alias in node.names:
                dotted = f"{parent}.{alias.name}" if parent else alias.name
                if dotted in packages:
                    aliases[alias.asname or alias.name] = dotted
    return aliases


def _module_of(node: ast.expr, module: _Module, packages: set[str]) -> str | None:
    """Return the scanned module or package an expression names, if any."""
    if isinstance(node, ast.Name):
        return module.module_aliases.get(node.id)
    if isinstance(node, ast.Attribute):
        parent = _module_of(node.value, module, packages)
        dotted = f"{parent}.{node.attr}" if parent else None
        return dotted if dotted in packages else None
    return None


def _exported_table_attribute(
    node: ast.Attribute, module: _Module, modules: Mapping[str, _Module]
) -> bool:
    """Whether ``node`` is a Core table reached as an attribute of a module."""
    named_module = _module_of(node.value, module, module.packages)
    source = modules.get(named_module or "")
    return source is not None and node.attr in source.exports


def _packages(modules: Mapping[str, _Module]) -> set[str]:
    return {
        ".".join(name.split(".")[:length])
        for name in modules
        for length in range(1, name.count(".") + 2)
    }


def _imported_tables(module: _Module, modules: Mapping[str, _Module]) -> set[str]:
    """Return the Core table names the module imports.

    A name imported from a module that exports a Core table under it is one,
    and so is every name a star import brings in.
    """
    names: set[str] = set()
    module.star_names = set()
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        source = modules.get(module.resolve(node) or "")
        if source is None:
            continue
        for alias in node.names:
            if alias.name == STAR_IMPORT:
                names |= source.exports
                module.star_names |= source.exports
            elif alias.name in source.exports:
                names.add(alias.asname or alias.name)
    return names


def _class_body_statements(tree: ast.Module) -> set[int]:
    """Return the assignments made directly in a class body."""
    return {
        id(statement)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for statement in node.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
    }


def _split_targets(
    targets: Iterable[ast.expr], *, in_class_body: bool
) -> tuple[set[str], set[str]]:
    """Return the (names, attributes) the targets bind.

    A plain name bound in a class body is the class's attribute.
    """
    names: set[str] = set()
    attributes: set[str] = set()
    for target in targets:
        bound = set(_bound_names(target))
        attributes.update(_bound_attributes(target))
        if in_class_body:
            attributes |= bound
        else:
            names |= bound
    return names, attributes


def _annotated_as(module: _Module, types: frozenset[str]) -> tuple[set[str], set[str]]:
    """Return the (names, attributes) annotated with one of ``types``."""
    class_body = _class_body_statements(module.tree)
    names: set[str] = set()
    attributes: set[str] = set()
    for node in ast.walk(module.tree):
        if isinstance(node, ast.arg):
            if types & _annotation_names(node.annotation, module.imported):
                names.add(node.arg)
        elif isinstance(node, ast.AnnAssign) and types & _annotation_names(
            node.annotation, module.imported
        ):
            found = _split_targets([node.target], in_class_body=id(node) in class_body)
            names |= found[0]
            attributes |= found[1]
    return names, attributes


def _bound_to_calls(
    module: _Module, functions: frozenset[str]
) -> tuple[set[str], set[str]]:
    """Return the (names, attributes) bound to a call of one of ``functions``."""
    class_body = _class_body_statements(module.tree)
    names: set[str] = set()
    attributes: set[str] = set()
    for node, targets, value in _assignment_nodes(module.tree):
        if (
            isinstance(value, ast.Call)
            and _sqlalchemy_name(value.func, module.imported) in functions
        ):
            found = _split_targets(targets, in_class_body=id(node) in class_body)
            names |= found[0]
            attributes |= found[1]
    return names, attributes


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


def _assignment_nodes(
    tree: ast.AST,
) -> Iterator[tuple[ast.stmt | ast.expr, list[ast.expr], ast.expr]]:
    """Yield each assignment with its targets and value, pairing tuples.

    ``a, b = x, y`` yields ``a`` with ``x`` and ``b`` with ``y``.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for part, value in _paired(target, node.value):
                    yield node, [part], value
        elif (isinstance(node, ast.AnnAssign) and node.value is not None) or isinstance(
            node, ast.NamedExpr
        ):
            yield node, [node.target], node.value


def _names_and_attributes(found: tuple[set[str], set[str]]) -> set[str]:
    return found[0] | found[1]


def _bound_to_mappers(module: _Module) -> set[str]:
    """Return the names and attributes assigned a mapper, as in ``m = X.__mapper__``."""
    return {
        name
        for _, targets, value in _assignment_nodes(module.tree)
        if isinstance(value, ast.Attribute) and value.attr in MAPPER_NAMES
        for target in targets
        for name in (*_bound_names(target), *_bound_attributes(target))
    }


def _is_core_expression(
    value: ast.expr,
    module: _Module,
    modules: Mapping[str, _Module],
    tables: tuple[set[str], set[str]],
) -> bool:
    """Whether an expression is rooted at a Core table, through any attributes.

    ``__table__`` and the other attributes in ``TABLE_ATTRIBUTES`` yield a Core
    table whatever they are read from, so a name assigned from one, such as
    ``household_jobs = HouseholdJob.__table__``, stays a Core table.
    """
    names, attributes = tables
    node = value
    while True:
        if isinstance(node, ast.Attribute):
            if (
                node.attr in TABLE_ATTRIBUTES
                or node.attr in attributes
                or _exported_table_attribute(node, module, modules)
            ):
                return True
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, (ast.Subscript, ast.Starred)):
            node = node.value
        else:
            return isinstance(node, ast.Name) and node.id in names


def _core_tables(module: _Module, modules: Mapping[str, _Module]) -> None:
    """Record the names and attributes that hold a Core table within the module.

    A class or instance attribute counts only within the module that sets it:
    another module reading it gets the table that a reported reference stored
    there, wherever that reference was made.
    """
    class_body = _class_body_statements(module.tree)
    annotated_names, annotated_attributes = _annotated_as(
        module, frozenset({TABLE_TYPE})
    )
    built_names, built_attributes = _bound_to_calls(module, TABLE_FACTORIES)
    names = _imported_tables(module, modules) | annotated_names | built_names
    attributes = annotated_attributes | built_attributes
    assignments = list(_assignment_nodes(module.tree))
    grew = True
    while grew:
        grew = False
        for node, targets, value in assignments:
            if not _is_core_expression(value, module, modules, (names, attributes)):
                continue
            new_names, new_attributes = _split_targets(
                targets, in_class_body=id(node) in class_body
            )
            if not new_names <= names or not new_attributes <= attributes:
                names |= new_names
                attributes |= new_attributes
                grew = True
    module.core_tables = names
    module.core_attributes = attributes


def _index_modules(sources: Mapping[str, str]) -> dict[str, _Module]:
    modules: dict[str, _Module] = {}
    for file, source in sorted(sources.items()):
        tree = ast.parse(source, filename=file)
        module = _Module(file=file, tree=tree, imported=_sqlalchemy_imports(tree))
        if module.name in modules:
            message = (
                f"{modules[module.name].file} and {file} are both module "
                f"{module.name}; the check resolves imports by module name"
            )
            raise ValueError(message)
        modules[module.name] = module
    packages = set(modules) | _packages(modules)
    for module in modules.values():
        module.packages = packages
        module.module_aliases = _module_aliases(module, packages)
    # A module exports the Core tables it binds or imports at module level;
    # imports chain through packages, so repeat until no export is added.
    # Exports only grow from one pass to the next, so the loop ends.
    grew = True
    while grew:
        grew = False
        for module in modules.values():
            previous = set(module.exports)
            _core_tables(module, modules)
            exported = _module_level_names(module.tree) | module.star_names
            module.exports = module.core_tables & exported
            if module.exports != previous:
                grew = True
    return modules


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
    modules: Mapping[str, _Module]
    metadata_names: set[str]
    mapper_names: set[str]
    engine_names: set[str]
    schema_details: set[int]
    called: set[int]


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


def _names_core_table(node: ast.Attribute, context: _ModuleContext) -> bool:
    if isinstance(node.ctx, ast.Load) and node.attr in context.module.core_attributes:
        return True
    return _exported_table_attribute(node, context.module, context.modules)


def _attribute_findings(
    node: ast.Attribute, context: _ModuleContext
) -> Iterator[tuple[str, str]]:
    if _names_core_table(node, context) or _reaches_mapped_table(node, context):
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
    findings: list[Finding] = []
    for module in modules.values():
        context = _ModuleContext(
            module=module,
            modules=modules,
            metadata_names=_names_and_attributes(
                _bound_to_calls(module, frozenset({METADATA_CONSTRUCTOR}))
            ),
            mapper_names=_names_and_attributes(
                _bound_to_calls(module, MAPPER_FUNCTIONS)
            )
            | _bound_to_mappers(module),
            engine_names=_names_and_attributes(
                _bound_to_calls(module, ENGINE_FACTORIES)
            )
            | _names_and_attributes(_annotated_as(module, ENGINE_TYPES)),
            schema_details=_schema_detail_calls(module.tree, module.imported),
            called={
                id(node.func)
                for node in ast.walk(module.tree)
                if isinstance(node, ast.Call)
            },
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

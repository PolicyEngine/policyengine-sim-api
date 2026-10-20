"""Checks behind the repository's database access rules in rules/registry.toml.

``DDL_PATTERN`` is shared: sim.stage12.no-schema-ddl matches it against whole
Stage 12 runtime files, and sim.boundaries.sqlalchemy-and-alembic-only matches
it against the string literals of every source file. ``database_access_findings``
holds the second rule's checks, so the rule test runs it on the repository and
the proof test runs it on the fixtures under ``rules/fixtures/<rule id>/``.

The checks read source, not types. They recognise a table as a ``Table(...)``
call, a name or attribute bound to one or annotated as ``Table`` in any of the
scanned files, a ``__table__`` attribute or an item of ``.tables``. They don't
follow imports made through ``importlib`` or ``__import__``, tables reached
through other local variables, or SQL text assembled from separate literals.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

# Schema DDL as SQL text, and metadata creation.
DDL_PATTERN = re.compile(
    r"\b(?:CREATE|ALTER|DROP|TRUNCATE)\s+"
    r"(?:TABLE|TYPE|SCHEMA|DATABASE)\b|\bcreate_all\s*\(",
    flags=re.IGNORECASE,
)

# Modules that open database connections without SQLAlchemy. A module matches
# itself and every submodule, so psycopg covers psycopg.rows.
DATABASE_DRIVER_MODULES = frozenset(
    {
        "aiomysql",
        "aiosqlite",
        "asyncpg",
        "MySQLdb",
        "mysql.connector",
        "pg8000",
        "psycopg",
        "psycopg2",
        "pymysql",
        "sqlite3",
    }
)
SCHEMA_MIGRATION_MODULE = "alembic"
# A directory holding an Alembic chain; only files under it may import alembic
# or change the schema.
MIGRATIONS_DIRECTORY = "migrations"
METADATA_SCHEMA_METHODS = frozenset({"create_all", "drop_all"})
TABLE_SCHEMA_METHODS = frozenset({"create", "drop"})
# SQLAlchemy's create and drop methods take checkfirst; pandas' don't.
SCHEMA_METHOD_KEYWORD = "checkfirst"
TABLE_CLASS = "Table"
TABLE_ATTRIBUTE = "__table__"
METADATA_TABLES_ATTRIBUTE = "tables"
# SQLAlchemy statements that create, drop or alter schema objects when executed.
SQLALCHEMY_DDL_CONSTRUCTS = frozenset(
    {
        "AddConstraint",
        "CreateIndex",
        "CreateSchema",
        "CreateSequence",
        "CreateTable",
        "DDL",
        "DropConstraint",
        "DropIndex",
        "DropSchema",
        "DropSequence",
        "DropTable",
    }
)

DRIVER_IMPORT = "database driver import"
ALEMBIC_IMPORT = "alembic import outside a migrations directory"
SCHEMA_CHANGE_CALL = "schema change call"
DDL_CONSTRUCT = "SQLAlchemy DDL construct"
DDL_TEXT = "DDL text"


@dataclass(frozen=True)
class Finding:
    """One way a source file reaches a database or changes its schema."""

    file: str
    line: int
    check: str
    source: str

    def __str__(self) -> str:
        """Describe the finding as ``file:line: check: source``."""
        return f"{self.file}:{self.line}: {self.check}: {self.source}"


def _matches_module(module_name: str, forbidden_module: str) -> bool:
    return module_name == forbidden_module or module_name.startswith(
        f"{forbidden_module}."
    )


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level or node.module is None:
        return []
    # ``from mysql import connector`` imports mysql.connector.
    return [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]


def _called_attribute(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _annotates_table(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value.rsplit(".", 1)[-1] == TABLE_CLASS
    return _called_attribute(annotation) == TABLE_CLASS


def _is_table_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and _called_attribute(node.func) == TABLE_CLASS


def _bound_names(target: ast.expr) -> list[str]:
    if isinstance(target, (ast.Name, ast.Attribute)):
        return [_called_attribute(target)]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    return []


def table_names(tree: ast.Module) -> frozenset[str]:
    """Return the names a module binds to a ``Table(...)`` or annotates as one."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_table_call(node.value):
            names.update(
                name for target in node.targets for name in _bound_names(target)
            )
        elif isinstance(node, ast.AnnAssign) and (
            _annotates_table(node.annotation)
            or (node.value is not None and _is_table_call(node.value))
        ):
            names.update(_bound_names(node.target))
        elif isinstance(node, ast.arg) and _annotates_table(node.annotation):
            names.add(node.arg)
    return frozenset(names)


def _is_table(node: ast.expr, known_tables: frozenset[str]) -> bool:
    if _is_table_call(node):
        return True
    if isinstance(node, ast.Attribute) and node.attr == TABLE_ATTRIBUTE:
        return True
    if isinstance(node, ast.Subscript):
        return _called_attribute(node.value) == METADATA_TABLES_ATTRIBUTE
    return _called_attribute(node) in known_tables


def _changes_schema(call: ast.Call, known_tables: frozenset[str]) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    method = call.func.attr
    if method in METADATA_SCHEMA_METHODS:
        return True
    if method not in TABLE_SCHEMA_METHODS:
        return False
    return _is_table(call.func.value, known_tables) or any(
        keyword.arg == SCHEMA_METHOD_KEYWORD for keyword in call.keywords
    )


def _docstring_nodes(tree: ast.Module) -> set[int]:
    documented = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, documented) or not node.body:
            continue
        first_statement = node.body[0]
        if isinstance(first_statement, ast.Expr) and isinstance(
            first_statement.value, ast.Constant
        ):
            docstrings.add(id(first_statement.value))
    return docstrings


def _is_in_migrations(file: str) -> bool:
    return MIGRATIONS_DIRECTORY in PurePosixPath(file).parts[:-1]


def _import_findings(
    node: ast.Import | ast.ImportFrom, *, in_migrations: bool
) -> Iterator[str]:
    modules = _imported_modules(node)
    if any(
        _matches_module(module, driver)
        for module in modules
        for driver in DATABASE_DRIVER_MODULES
    ):
        yield DRIVER_IMPORT
    if not in_migrations and any(
        _matches_module(module, SCHEMA_MIGRATION_MODULE) for module in modules
    ):
        yield ALEMBIC_IMPORT
    if (
        not in_migrations
        and isinstance(node, ast.ImportFrom)
        and node.module is not None
        and _matches_module(node.module, "sqlalchemy")
        and any(alias.name in SQLALCHEMY_DDL_CONSTRUCTS for alias in node.names)
    ):
        yield DDL_CONSTRUCT


def _schema_findings(
    node: ast.AST, docstrings: set[int], known_tables: frozenset[str]
) -> Iterator[str]:
    if isinstance(node, ast.Call) and _changes_schema(node, known_tables):
        yield SCHEMA_CHANGE_CALL
    elif isinstance(node, ast.Attribute) and node.attr in SQLALCHEMY_DDL_CONSTRUCTS:
        yield DDL_CONSTRUCT
    elif (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and DDL_PATTERN.search(node.value)
    ):
        yield DDL_TEXT


def _module_findings(
    file: str, tree: ast.Module, known_tables: frozenset[str]
) -> Iterator[Finding]:
    in_migrations = _is_in_migrations(file)
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            checks = list(_import_findings(node, in_migrations=in_migrations))
        elif in_migrations:
            checks = []
        else:
            checks = list(_schema_findings(node, docstrings, known_tables))
        for check in checks:
            yield Finding(file, getattr(node, "lineno", 0), check, ast.unparse(node))


def database_access_findings(sources: Mapping[str, str]) -> list[Finding]:
    """Return every driver import, stray alembic import and schema change.

    ``sources`` maps a repository-relative file name to its source. Tables
    bound in one file are recognised in all of them, since modules import their
    tables from a shared module. Files under a ``migrations`` directory may
    import alembic and change the schema; every file must reach the database
    through SQLAlchemy.
    """
    trees = {file: ast.parse(source, filename=file) for file, source in sources.items()}
    known_tables = frozenset().union(*(table_names(tree) for tree in trees.values()))
    findings = [
        finding
        for file, tree in trees.items()
        for finding in _module_findings(file, tree, known_tables)
    ]
    return sorted(findings, key=lambda finding: (finding.file, finding.line))

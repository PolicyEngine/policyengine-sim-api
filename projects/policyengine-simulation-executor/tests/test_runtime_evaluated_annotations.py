"""Pydantic models keep the runtime imports their annotations need.

The packages' modules use ``from __future__ import annotations``, so ruff's
typing-only import rules (TC) move an import that only annotations use under
``if TYPE_CHECKING:``. Pydantic evaluates model annotations at runtime, so ruff
must treat every model as runtime-evaluated. It does when a base of the model,
read through the imports of the model's own module, is listed in
``runtime-evaluated-base-classes`` in ruff.toml, or is a class defined in that
same module that is runtime-evaluated itself. Ruff never follows a re-export to
the module that defines a class, so these checks resolve bases exactly as ruff
does. See docs/engineering/skills/typing-only-imports.md.

The entrypoint's test suite keeps a copy of the resolution helpers, because
the two projects share no package that test helpers could live in.
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import importlib
import importlib.util
import pkgutil
import sys
import tomllib
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import pydantic
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
# The executor and every project library installed in its environment.
CHECKED_PACKAGES = (
    "policyengine_fastapi",
    "policyengine_simulation_contract",
    "policyengine_simulation_executor",
    "policyengine_simulation_observability",
    "policyengine_stage12_persistence",
)
# src/modal holds the Modal entry files. They are not part of the installed
# package, and importing them builds Modal images and functions, which the tests
# that cover them do only with a fake Modal SDK and a patched environment. They
# are checked statically instead, by test_modal_entry_files_define_no_models.
MODAL_ENTRY_DIRECTORY = SOURCE_ROOT / "modal"

# What a module-level name is bound to: the qualified name an import gives it,
# a class statement in the same module, or None when ruff cannot resolve it (an
# assignment or a function definition).
Binding = str | ast.ClassDef | None


@dataclasses.dataclass(frozen=True)
class ModuleClass:
    """A module-level class statement, with the names bound where it runs."""

    module_name: str
    ruff_module_path: str
    node: ast.ClassDef
    bindings: dict[str, Binding]

    @property
    def qualified_name(self) -> str:
        return f"{self.module_name}.{self.node.name}"


@dataclasses.dataclass(frozen=True)
class ResolvedBase:
    """A base as ruff reads it, and whether ruff follows it within the module."""

    written: str
    path: str | None
    same_module: bool


def _module_level_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield the statements that run at module level, in source order."""
    for statement in body:
        yield statement
        if isinstance(statement, ast.If):
            yield from _module_level_statements(statement.body)
            yield from _module_level_statements(statement.orelse)
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            for block in (
                statement.body,
                *(handler.body for handler in statement.handlers),
                statement.orelse,
                statement.finalbody,
            ):
                yield from _module_level_statements(block)


def _names_bound_by(statement: ast.stmt, package: str) -> dict[str, Binding]:
    """Return the module-level names a statement binds, as ruff resolves them."""
    if isinstance(statement, ast.Import):
        bound: dict[str, Binding] = {}
        for alias in statement.names:
            if alias.asname is None:
                top_level = alias.name.partition(".")[0]
                bound[top_level] = top_level
            else:
                bound[alias.asname] = alias.name
        return bound
    if isinstance(statement, ast.ImportFrom):
        source_module = (
            importlib.util.resolve_name(
                "." * statement.level + (statement.module or ""), package
            )
            if statement.level
            else statement.module
        )
        return {
            alias.asname or alias.name: f"{source_module}.{alias.name}"
            for alias in statement.names
            if alias.name != "*"
        }
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {statement.name: None}
    if isinstance(statement, ast.Assign) or (
        isinstance(statement, ast.AnnAssign) and statement.value is not None
    ):
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        return {
            node.id: None
            for target in targets
            for node in ast.walk(target)
            if isinstance(node, ast.Name)
        }
    return {}


def module_classes(
    module_name: str, source: str, *, is_package: bool
) -> dict[str, ModuleClass]:
    """Return each module-level class with the names bound at its statement.

    Ruff names a module after its file, so a package's ``__init__.py`` is
    ``package.__init__`` to ruff.
    """
    package = module_name if is_package else module_name.rpartition(".")[0]
    ruff_module_path = f"{module_name}.__init__" if is_package else module_name
    bindings: dict[str, Binding] = {}
    classes: dict[str, ModuleClass] = {}
    for statement in _module_level_statements(ast.parse(source).body):
        if isinstance(statement, ast.ClassDef):
            classes[statement.name] = ModuleClass(
                module_name, ruff_module_path, statement, dict(bindings)
            )
            bindings[statement.name] = statement
        else:
            bindings.update(_names_bound_by(statement, package))
    return classes


def _dotted_name(base: ast.expr) -> list[str] | None:
    """Split ``a.b.Base`` into its names, reading ``Base[Item]`` as ``Base``."""
    if isinstance(base, ast.Subscript):
        base = base.value
    attributes: list[str] = []
    while isinstance(base, ast.Attribute):
        attributes.insert(0, base.attr)
        base = base.value
    if not isinstance(base, ast.Name):
        return None
    return [base.id, *attributes]


def _ruff_path(module_class: ModuleClass, base: ast.expr) -> str | None:
    """Return the qualified name ruff resolves a base to, through the imports."""
    names = _dotted_name(base)
    if names is None:
        return None
    head, *attributes = names
    if head not in module_class.bindings:
        return ".".join(["builtins", *names]) if hasattr(builtins, head) else None
    binding = module_class.bindings[head]
    if binding is None:
        return None
    if isinstance(binding, ast.ClassDef):
        binding = f"{module_class.ruff_module_path}.{head}"
    return ".".join([binding, *attributes])


def resolved_bases(
    module_class: ModuleClass, followed: frozenset[str] = frozenset()
) -> list[ResolvedBase]:
    """Return the bases ruff checks, following classes in the same module.

    Ruff resolves a same-module base's own bases with the names bound at the
    subclass, so the subclass's bindings are kept on the way down.
    """
    found: list[ResolvedBase] = []
    for base in module_class.node.bases:
        names = _dotted_name(base)
        local_class = (
            module_class.bindings.get(names[0])
            if names is not None and len(names) == 1
            else None
        )
        same_module = isinstance(local_class, ast.ClassDef)
        found.append(
            ResolvedBase(ast.unparse(base), _ruff_path(module_class, base), same_module)
        )
        if isinstance(local_class, ast.ClassDef) and local_class.name not in followed:
            found += resolved_bases(
                dataclasses.replace(module_class, node=local_class),
                followed | {local_class.name},
            )
    return found


def is_runtime_evaluated(module_class: ModuleClass, listed: frozenset[str]) -> bool:
    """Whether ruff treats the class's annotations as evaluated at runtime."""
    return any(base.path in listed for base in resolved_bases(module_class))


def _names_a_model(path: str) -> bool:
    try:
        target = pkgutil.resolve_name(path)
    except (AttributeError, ImportError, ValueError):
        return False
    return isinstance(target, type) and issubclass(target, pydantic.BaseModel)


def _bases_to_list(module_class: ModuleClass) -> list[str]:
    """Return the model bases from other modules that would fix the class."""
    outside = [
        base.path or base.written
        for base in resolved_bases(module_class)
        if not base.same_module
    ]
    return [path for path in outside if _names_a_model(path)] or outside


def _listed_runtime_evaluated_bases() -> frozenset[str]:
    with (REPOSITORY_ROOT / "ruff.toml").open("rb") as ruff_config:
        settings = tomllib.load(ruff_config)
    type_checking = settings["lint"]["flake8-type-checking"]
    return frozenset(type_checking["runtime-evaluated-base-classes"])


@cache
def _imported_modules() -> tuple[ModuleType, ...]:
    modules = []
    for package_name in CHECKED_PACKAGES:
        package = importlib.import_module(package_name)
        modules.append(package)
        modules += [
            importlib.import_module(module_info.name)
            for module_info in pkgutil.walk_packages(
                package.__path__, f"{package_name}."
            )
        ]
    return tuple(modules)


@cache
def _model_classes() -> tuple[tuple[ModuleClass, type[pydantic.BaseModel]], ...]:
    """Return each Pydantic model a module-level class statement defines."""
    models = []
    for module in _imported_modules():
        source_path = Path(module.__file__)
        classes = module_classes(
            module.__name__,
            source_path.read_text(encoding="utf-8"),
            is_package=source_path.name == "__init__.py",
        )
        for class_name, module_class in classes.items():
            runtime_class = vars(module).get(class_name)
            if (
                isinstance(runtime_class, type)
                and issubclass(runtime_class, pydantic.BaseModel)
                and runtime_class.__module__ == module.__name__
            ):
                models.append((module_class, runtime_class))
    return tuple(models)


def _unresolved_annotation(model: type[pydantic.BaseModel]) -> str | None:
    try:
        model.model_rebuild(raise_errors=True)
    except pydantic.PydanticUndefinedAnnotation as error:
        return str(error)
    return None


def test_every_model_is_runtime_evaluated_for_ruff() -> None:
    listed = _listed_runtime_evaluated_bases()
    models = _model_classes()
    missing = [
        f"{module_class.qualified_name}: add {_bases_to_list(module_class)}"
        for module_class, _ in models
        if not is_runtime_evaluated(module_class, listed)
    ]

    assert models, "found no Pydantic models to check"
    assert missing == [], (
        "ruff would not treat these Pydantic models as runtime-evaluated, so it "
        "would move imports their annotations need under TYPE_CHECKING. Ruff "
        "reads a base through the imports of the model's own module, so add the "
        "base path named to runtime-evaluated-base-classes in ruff.toml, or "
        "import the base from a path listed there:\n" + "\n".join(missing)
    )


def test_every_model_resolves_its_annotations() -> None:
    unresolved = [
        f"{module_class.qualified_name}: {message}"
        for module_class, model in _model_classes()
        if (message := _unresolved_annotation(model)) is not None
    ]

    assert unresolved == [], (
        "these models use names in annotations that don't exist at runtime, "
        "usually an import moved under TYPE_CHECKING for a model ruff does not "
        "treat as runtime-evaluated:\n" + "\n".join(unresolved)
    )


def test_modal_entry_files_define_no_models() -> None:
    """Every class in src/modal derives only from builtins and the standard library."""
    standard_library = sys.stdlib_module_names
    possible_models = []
    for source_path in sorted(MODAL_ENTRY_DIRECTORY.rglob("*.py")):
        module_path = source_path.relative_to(SOURCE_ROOT).with_suffix("").parts
        is_package = module_path[-1] == "__init__"
        classes = module_classes(
            ".".join(module_path[:-1] if is_package else module_path),
            source_path.read_text(encoding="utf-8"),
            is_package=is_package,
        )
        possible_models += [
            f"{source_path.relative_to(REPOSITORY_ROOT)}: {module_class.node.name}"
            f" derives from {base.path or base.written}"
            for module_class in classes.values()
            for base in resolved_bases(module_class)
            if not base.same_module
            and (base.path or "").partition(".")[0] not in standard_library
        ]

    assert possible_models == [], (
        "these checks do not import src/modal, so a Pydantic model there would "
        "go unchecked; define it in policyengine_simulation_executor instead:\n"
        + "\n".join(possible_models)
    )


def test_rule_proof_runtime_evaluated_models_resolve() -> None:
    """Proof for rule sim.quality.runtime-evaluated-models-resolve.

    The rule is registered in rules/registry.toml.
    """
    fixtures = (
        REPOSITORY_ROOT / "rules/fixtures/sim.quality.runtime-evaluated-models-resolve"
    )
    listed = _listed_runtime_evaluated_bases()
    verdicts = {
        fixture_name: is_runtime_evaluated(
            module_classes(
                "policyengine_simulation_executor.compat_models",
                (fixtures / fixture_name).read_text(encoding="utf-8"),
                is_package=False,
            )["EconomyComparison"],
            listed,
        )
        for fixture_name in ("bad_compat_models.py", "good_compat_models.py")
    }

    assert verdicts == {"bad_compat_models.py": False, "good_compat_models.py": True}


RESOLUTION_LISTED = frozenset(
    {"pkg.models.Base", "pydantic.BaseModel", "pydantic.RootModel"}
)


@pytest.mark.parametrize(
    ("source", "expected_paths", "runtime_evaluated"),
    [
        pytest.param(
            "from pydantic import BaseModel\n\nclass Model(BaseModel): ...",
            ["pydantic.BaseModel"],
            True,
            id="listed-base",
        ),
        pytest.param(
            "from pydantic import RootModel\n\nclass Model(RootModel[int]): ...",
            ["pydantic.RootModel"],
            True,
            id="listed-base-subscripted",
        ),
        pytest.param(
            "from ..models import Base\n\nclass Model(Base): ...",
            ["pkg.models.Base"],
            True,
            id="listed-base-relative-import",
        ),
        pytest.param(
            "from typing import Generic, TypeVar\n"
            "from pydantic import BaseModel\n"
            "Item = TypeVar('Item')\n"
            "class _Root(BaseModel, Generic[Item]): ...\n"
            "class _Middle(_Root[Item]): ...\n"
            "class Model(_Middle[int]): ...",
            [
                "pkg.service.api._Middle",
                "pkg.service.api._Root",
                "pydantic.BaseModel",
                "typing.Generic",
            ],
            True,
            id="same-module-chain",
        ),
        pytest.param(
            "from pkg.reexports import Base\n\nclass Model(Base): ...",
            ["pkg.reexports.Base"],
            False,
            id="unlisted-re-export",
        ),
        pytest.param(
            "from pydantic_settings import BaseSettings\n\n"
            "class Model(BaseSettings): ...",
            ["pydantic_settings.BaseSettings"],
            False,
            id="unlisted-third-party-base",
        ),
        pytest.param(
            "from pydantic import BaseModel\nBase = BaseModel\nclass Model(Base): ...",
            [None],
            False,
            id="assigned-alias-unresolved",
        ),
    ],
)
def test_bases_resolve_as_ruff_resolves_them(source, expected_paths, runtime_evaluated):
    model = module_classes("pkg.service.api", source, is_package=False)["Model"]

    assert [base.path for base in resolved_bases(model)] == expected_paths
    assert is_runtime_evaluated(model, RESOLUTION_LISTED) is runtime_evaluated


def test_same_module_base_in_a_package_is_named_after_its_file() -> None:
    """Ruff names a class in ``__init__.py`` with ``__init__`` in its path."""
    source = (
        "from somewhere import Unlisted\n\n"
        "class Base(Unlisted): ...\n"
        "class Model(Base): ...\n"
    )
    model = module_classes("pkg.service", source, is_package=True)["Model"]

    assert is_runtime_evaluated(model, frozenset({"pkg.service.__init__.Base"}))
    assert not is_runtime_evaluated(model, frozenset({"pkg.service.Base"}))

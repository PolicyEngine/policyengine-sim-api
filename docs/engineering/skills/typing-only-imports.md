# Typing-Only Imports Skill

Read this before adding a Pydantic model, before adding a class that other
models inherit from, or before moving an import under `if TYPE_CHECKING:`.

## When an import moves

The package modules use `from __future__ import annotations`, so Python stores
every annotation as a string and never evaluates it. An import that only
annotations use is therefore not needed at runtime, and ruff's typing-only
import rules (`TC`) move it under `if TYPE_CHECKING:`, which is `False` when the
code runs and `True` for type checkers. That removes import-time work and
import cycles without changing behaviour.

## Models that read annotations at runtime

Some code does evaluate those strings at runtime:

- Pydantic builds each model's validators from its annotations, including
  `pydantic-settings` settings classes.
- FastAPI reads endpoint signatures when routes are registered.

Names those annotations use must exist at runtime. `[lint.flake8-type-checking]`
in `ruff.toml` tells ruff which classes and functions these are:
`runtime-evaluated-base-classes` lists model base classes, and
`runtime-evaluated-decorators` lists the FastAPI route decorators. Ruff keeps an
import at runtime when a runtime-evaluated class or endpoint uses it in an
annotation.

If ruff does not treat a model as runtime-evaluated, it moves the model's
annotation-only imports under `TYPE_CHECKING`, and Pydantic raises "not fully
defined" the first time the model is used.

## How ruff decides a class is runtime-evaluated

Ruff reads each base of a class statement through the import statements of the
module that holds the class, and compares the qualified name it gets with
`runtime-evaluated-base-classes`:

- `from pydantic import BaseModel` makes the base `BaseModel` read as
  `pydantic.BaseModel`, and so does `pydantic.BaseModel` after
  `import pydantic`. A relative import resolves against the module's package.
- A subscripted base reads as the class it subscripts: `RootModel[Item]` is
  `pydantic.RootModel` and `Generic[Item]` is `typing.Generic`.
- A base defined in the same module counts when it is runtime-evaluated itself.
  Ruff follows inheritance through classes in the same module, however many
  levels there are.
- Ruff follows nothing else. It never looks inside the module an imported base
  comes from, so a base imported through a re-export reads as the re-exporting
  path, and a model that subclasses a model from another module is covered only
  when that base's import path is listed. A name bound by an assignment, such as
  `Base = BaseModel`, does not resolve at all.

Ruff does not check whether a class is a Pydantic model: listing the right path
is the only signal it gets.

## Adding a model or a model base

- Subclass a project model by importing it from the module that defines it, and
  list that path, the defining module then the class name. Don't subclass
  through a re-export such as the executor's `simulation_macro_output`
  compatibility module: the re-exported path is not listed.
- When you add a model class that models in other modules inherit from, add its
  path to `runtime-evaluated-base-classes` in the same change.
- A third-party base is listed by the public path its subclasses import it from,
  such as `pydantic_settings.BaseSettings` or policyengine.py's
  `policyengine.core.Simulation`.
- A base used only by models in its own module needs no entry, as long as it
  derives from a listed class.

If ruff reports a typing-only import in a module that defines a model, check the
model is runtime-evaluated before moving the import. Moving it for a model ruff
misses breaks the model at runtime.

## The guard

`sim.quality.runtime-evaluated-models-resolve` runs
`tests/test_runtime_evaluated_annotations.py` in two test suites:

- the executor's, over the executor package and the project libraries installed
  with it (`policyengine_fastapi`, `policyengine_simulation_contract`,
  `policyengine_simulation_observability` and
  `policyengine_stage12_persistence`);
- the entrypoint's, over `policyengine_simulation_entry` and
  `policyengine_simulation_persistence`, which only the entrypoint installs.

Each imports every module of its packages and checks every Pydantic model a
module-level class statement defines:

- the model is runtime-evaluated, with its bases resolved as ruff resolves them
  above; a failure names the model and the base path to list;
- the model resolves its annotations, with `model_rebuild(raise_errors=True)`.

The gateway defines no Pydantic models; it uses the contract's. The executor's
`src/modal` directory holds Modal entry files, which are not part of the
installed package and build Modal images and functions when imported, so the
guard does not import them; it checks statically that every class there derives
only from builtins and the standard library, so none can be a Pydantic model.

The two projects share no package that test helpers could live in, so the
entrypoint's test keeps a copy of the executor's resolution helpers. Change both
copies together.

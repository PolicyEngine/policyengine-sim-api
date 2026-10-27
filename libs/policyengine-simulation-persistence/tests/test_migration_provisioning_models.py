"""Every Pydantic model of migration provisioning resolves its annotations.

The repository's runtime-evaluated-models guard imports other packages, so
this module checks the provisioning models the same way: each module imports
cleanly, and each model it defines builds its validators.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from policyengine_simulation_persistence import migration_provisioning
from pydantic import BaseModel

MODULE_NAMES = sorted(
    module.name
    for module in pkgutil.iter_modules(
        migration_provisioning.__path__, migration_provisioning.__name__ + "."
    )
)


def test_every_module_is_scanned() -> None:
    assert [name.rsplit(".", 1)[1] for name in MODULE_NAMES] == [
        "__main__",
        "command",
        "definitions",
        "mapping",
        "provisioning",
    ]


@pytest.mark.parametrize("module_name", MODULE_NAMES)
def test_importing_a_module_runs_nothing_and_its_models_resolve(
    module_name: str,
) -> None:
    module = importlib.import_module(module_name)

    models = [
        member
        for _, member in inspect.getmembers(module, inspect.isclass)
        if issubclass(member, BaseModel) and member.__module__ == module_name
    ]
    for model in models:
        model.model_rebuild(force=True, raise_errors=True)

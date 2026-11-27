"""Find a v2 simulation worker in a deployed application, by its method's name."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.stage12_simulation_workers import (
    worker_class_name,
)

if TYPE_CHECKING:
    from types import ModuleType


def deployed_worker(
    modal_module: ModuleType,
    application_name: str,
    method_name: str,
    **lookup_options: str,
) -> Any:  # noqa: ANN401  # a modal.Function or a Modal class's method, both untyped here
    """Return what spawns ``method_name`` in ``application_name``.

    A simulation worker is a method of a Modal class, whose containers restore
    from a memory snapshot with the models loaded. An application deployed
    from a commit from before the classes defines it as a plain function of
    the same name, and a call still running when its application is rolled
    back in place to such a commit finds only that function. So a class Modal
    doesn't find gives the function. Only the class lookup falls back: once
    the class resolves, a failed spawn raises as it is. A name with no class
    is looked up as a function. ``lookup_options``, such as
    ``environment_name``, go to either lookup.
    """
    class_name = worker_class_name(method_name)
    if class_name is not None:
        worker_class = modal_module.Cls.from_name(
            application_name, class_name, **lookup_options
        )
        try:
            worker_class.hydrate()
        except modal_module.exception.NotFoundError:
            pass
        else:
            return getattr(worker_class(), method_name)
    return modal_module.Function.from_name(
        application_name, method_name, **lookup_options
    )

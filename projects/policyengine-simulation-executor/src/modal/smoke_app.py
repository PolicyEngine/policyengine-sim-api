"""Pre-merge image smoke: import the executor runtime inside its image.

Import parity, not data parity: the image here is the deployed image's
layer prefix (pinned pip layer, policyengine bundle install, version
env) plus the source mounts — deliberately excluding the multi-hour
dataset-prebuild and model-snapshot layers, which add no Python
packages. Because layers are content-addressed and built through the
shared ``build_runtime_simulation_image()``, a warm cache makes this run
take seconds; after a relock it pays only the bundle install.

Runs the imports the deployed workers perform lazily at request time —
``run_simulation_impl``, the budget-window batch, both shared libs, and
the explicit ``import logfire`` (issue #602's crash path).

Usage:
    uv run modal run --env=staging src/modal/smoke_app.py
"""

import modal

from src.modal.app import build_runtime_simulation_image

app = modal.App("policyengine-simulation-executor-smoke")

smoke_image = build_runtime_simulation_image().add_local_python_source(
    "src.modal",
    "policyengine_simulation_executor",
    "policyengine_simulation_observability",
    "policyengine_simulation_contract",
    copy=True,
)


@app.function(image=smoke_image, timeout=600, memory=8192)
def smoke_import_executor() -> dict:
    import importlib
    import pkgutil

    # The #602 crash path: workers import logfire lazily inside
    # configure_logfire; import it explicitly here.
    import logfire  # noqa: F401

    # Module-level surface of the deployed app (versions resolve from the
    # baked env layer).
    importlib.import_module("src.modal.app")

    # The lazy request-time imports of the two worker functions.
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: F401
        run_simulation_impl,
    )
    from src.modal.budget_window_batch import (  # noqa: F401
        run_budget_window_batch_impl,
    )

    import policyengine_simulation_contract
    import policyengine_simulation_observability

    imported = []
    for package in (
        policyengine_simulation_contract,
        policyengine_simulation_observability,
    ):
        for module in pkgutil.walk_packages(
            package.__path__, prefix=f"{package.__name__}."
        ):
            importlib.import_module(module.name)
            imported.append(module.name)

    return {"modules_imported": len(imported)}


@app.local_entrypoint()
def main():
    report = smoke_import_executor.remote()
    print(report)
    print("executor image smoke OK")

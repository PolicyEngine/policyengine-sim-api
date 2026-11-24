"""Load the country models before Modal takes the workers' memory snapshot.

``SimulationWorker`` and ``SimulationSegmentWorker`` in ``src/modal/app.py``
run :func:`preload_country_models` in their ``@modal.enter(snap=True)`` hook.
Modal runs that hook once, in the container that takes the snapshot, and
every container restored from the snapshot starts with its results already in
memory:

- the country packages, whose import builds each country's tax-benefit
  system;
- policyengine.py's model objects for both countries, whose import also reads
  the data release manifest from Hugging Face over one HTTPS request that
  closes when it returns;
- the certified SPM capability and its forecast, which the first US call
  would otherwise load.

The preload configures no observability. Telemetry is configured per call,
after the restore, so each restored container reports its own process
identity.
"""

import logging
from importlib import import_module

from policyengine_simulation_contract.spm import SPMInputError

logger = logging.getLogger(__name__)

COUNTRY_MODEL_MODULES = (
    "policyengine_us",
    "policyengine_uk",
    "policyengine.tax_benefit_models.us",
    "policyengine.tax_benefit_models.uk",
)


def preload_country_models() -> None:
    """Import both countries' models, then fill the SPM capability cache.

    An unusable SPM setup is logged and left in place: ``runtime_spm_capability``
    caches only a successful result, so each call that needs SPM raises the
    same error and fails that request, as it would without the preload.
    """
    for module_name in COUNTRY_MODEL_MODULES:
        import_module(module_name)
    from policyengine_simulation_executor import spm

    try:
        spm.runtime_spm_capability()
    except SPMInputError as error:
        logger.warning(
            "SPM capability not preloaded; each call that needs it raises %s",
            error.code,
        )

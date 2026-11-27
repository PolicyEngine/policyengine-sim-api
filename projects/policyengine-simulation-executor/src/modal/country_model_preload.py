"""Load the country models before Modal takes the workers' memory snapshot.

``SimulationWorker`` and ``SimulationSegmentWorker`` in ``src/modal/app.py``,
and the v2 workers in ``src/modal/v2_app.py``, run
:func:`preload_country_models` in their ``@modal.enter(snap=True)`` hook.
Modal runs that hook in every container that isn't restored, and takes the
memory snapshot from such a container when it starts it with snapshots
enabled. Every container restored from the snapshot starts with the hook's
results already in memory:

- the country packages, whose import builds each country's tax-benefit
  system;
- policyengine.py's model objects for each country, whose import also reads
  the data release manifest from Hugging Face over one HTTPS request that
  closes when it returns;
- the certified SPM capability and its forecast, which the first US call
  would otherwise load.

The preload configures no observability. Telemetry is configured per call,
after the restore, so each restored container reports its own process
identity.
"""

import logging
from collections.abc import Iterable
from importlib import import_module

from policyengine_simulation_contract.spm import SPMInputError

logger = logging.getLogger(__name__)

COUNTRY_MODEL_MODULES = {
    "us": ("policyengine_us", "policyengine.tax_benefit_models.us"),
    "uk": ("policyengine_uk", "policyengine.tax_benefit_models.uk"),
}


def preload_country_models(countries: Iterable[str] = ("us", "uk")) -> None:
    """Import each of ``countries``' models, then fill the SPM capability cache.

    The run_simulation app's workers load both countries. Each of the v2
    app's workers loads only its image's country, and only the US workers
    fill the SPM cache, since SPM is a US measure.

    Nothing here may stop a container from starting: a failure leaves that
    work to the calls that need it, as before the preload. A country whose
    models fail to import is skipped, so the other country's calls keep their
    preloaded models, and Python discards a failed import, so each call of the
    skipped country imports its models again and fails as it would have. The
    SPM check caches only a successful result, so each call that needs SPM
    repeats a failed check.
    """
    countries = tuple(countries)
    for country, module_names in COUNTRY_MODEL_MODULES.items():
        if country not in countries:
            continue
        for module_name in module_names:
            try:
                import_module(module_name)
            except Exception:  # noqa: BLE001  # left to the country's own calls
                logger.warning(
                    "%s models not preloaded: importing %s failed, so each %s "
                    "call imports them itself",
                    country.upper(),
                    module_name,
                    country.upper(),
                    exc_info=True,
                )
                break
    if "us" not in countries:
        return
    from policyengine_simulation_executor import spm

    try:
        spm.runtime_spm_capability()
    except SPMInputError as error:
        logger.warning(
            "SPM capability not preloaded; each call that needs it raises %s",
            error.code,
        )
    except Exception:  # noqa: BLE001  # left to each call that needs SPM
        logger.warning(
            "SPM capability not preloaded; each call that needs it checks again",
            exc_info=True,
        )

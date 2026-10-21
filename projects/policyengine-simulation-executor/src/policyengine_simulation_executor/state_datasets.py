"""Where the main path's worker image keeps baked per-state datasets.

State and congressional-district regions load a regional dataset and keep
one state's rows. The precompute writes each state's filtered year file and
the image places it at ``<data folder>/state-datasets/<dataset>/<state>/``,
under the basename the dataset's own year file has. Every state therefore
gets its own folder, and ``ensure_datasets`` called on that folder finds the
state's file where it would look for the full year file.

The manifest builder and the runtime lookup both take folders from here, so
the folder an artifact is copied into is the folder a request reads.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine_simulation_executor.artifact_keys import dataset_artifact_filename

if TYPE_CHECKING:
    from policyengine_simulation_executor.simulation_runtime import DatasetSelection

logger = logging.getLogger(__name__)

STATE_DATASETS_DIRECTORY = "state-datasets"
# Values of the run's ``dataset_source`` attribute when its region loads a
# non-default dataset: read from the image's baked state file, or prepared
# from the full regional dataset during the request.
BAKED_STATE_DATASET_SOURCE = "baked_state"
ON_DEMAND_DATASET_SOURCE = "on_demand"


def state_dataset_relative_folder(dataset: str, state_code: str) -> str:
    """Return a state's folder relative to the image data folder."""
    return f"{STATE_DATASETS_DIRECTORY}/{dataset}/{state_code.upper()}"


def state_dataset_folder(data_folder: str, dataset: str, state_code: str) -> str:
    """Return the absolute folder of a state's baked year files."""
    return str(Path(data_folder) / state_dataset_relative_folder(dataset, state_code))


def year_file_reuse_problem(path: str | Path, dataset: str) -> str | None:
    """Return why ``ensure_datasets`` would rebuild this year file, or None.

    These are the two checks .py's ``ensure_datasets`` and ``load_datasets``
    run before reusing a year file of ``dataset``: the year-file stamp and
    renames record, then the legacy SPM record against the profile this
    release expects for the dataset.
    """
    from policyengine.tax_benefit_models.us.datasets import (
        _expected_legacy_spm_profile,
    )
    from policyengine.tax_benefit_models.us.legacy_inputs import (
        year_file_stamp_problem,
    )
    from policyengine.tax_benefit_models.us.legacy_spm.record import (
        year_file_problem,
    )

    problem = year_file_stamp_problem(path)
    if problem is not None:
        return problem
    return year_file_problem(path, _expected_legacy_spm_profile(dataset))


def baked_state_data_folder(
    *,
    country: str,
    selection: DatasetSelection,
    state_code: str | None,
    year: int,
    data_folder: str,
) -> str | None:
    """Return the baked folder a US state or district request should load, or None.

    A request qualifies when it loads a non-default dataset for a US state
    (a district passes its state's code) and the image holds that state's
    year file for ``year``. The file must also pass .py's reuse checks:
    otherwise ``ensure_datasets`` would rebuild the full regional dataset
    inside the baked folder, so the request goes to the on-demand path
    instead.
    """
    if country.lower() != "us" or selection.is_default or not state_code:
        return None
    from policyengine.provenance.manifest import (
        dataset_logical_name,
        resolve_dataset_reference,
    )

    folder = state_dataset_folder(data_folder, selection.name, state_code)
    stem = dataset_logical_name(resolve_dataset_reference("us", selection.name))
    year_file = Path(folder) / dataset_artifact_filename(stem, year)
    if not year_file.is_file():
        return None
    problem = year_file_reuse_problem(year_file, selection.name)
    if problem is not None:
        logger.warning("Not reading the baked state file %s: %s", year_file, problem)
        return None
    return folder

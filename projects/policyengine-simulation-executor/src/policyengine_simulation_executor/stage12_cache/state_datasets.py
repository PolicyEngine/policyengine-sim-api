"""Where the Stage 12 US worker image keeps baked per-state datasets.

Stage 12's own copy of the main path's per-state dataset layout (see
``policyengine_simulation_executor.state_datasets``): Stage 12 does not
import the main path's precompute modules. A state's year files sit at
``<Stage 12 data folder>/state-datasets/<dataset>/<state>/`` under the
basename the dataset's own year file has, so ``ensure_datasets`` called on
that folder finds the state's file where it would look for the full one.
"""

from __future__ import annotations

from pathlib import Path

STATE_DATASETS_DIRECTORY = "state-datasets"


def state_dataset_relative_folder(dataset: str, state_code: str) -> str:
    """Return a state's folder relative to the Stage 12 data folder."""
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

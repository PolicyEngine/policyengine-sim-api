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

from pathlib import Path

STATE_DATASETS_DIRECTORY = "state-datasets"


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

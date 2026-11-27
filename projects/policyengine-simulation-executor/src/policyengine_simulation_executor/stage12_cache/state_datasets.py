"""The year-file reuse checks .py runs, which the level handlers apply.

The level handlers (``levels_us``) write each state's year file and decide
whether an image's prebuilt year file is usable; both ask
``year_file_reuse_problem`` whether .py's ``ensure_datasets`` would reuse a
file or rebuild it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


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

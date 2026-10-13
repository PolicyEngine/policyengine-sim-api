"""Poverty rates keep "nobody measured" apart from a measured rate.

policyengine.py reports a ``None`` rate (NaN in its DataFrame) for a group
whose measured population is zero, for example a group made up entirely of
ACS group-quarters residents, whom Census's ACS SPM methodology does not
measure (PolicyEngine/policyengine.py#597).
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from policyengine_simulation_executor.simulation_output_poverty import (
    build_poverty_by_race_output,
    build_poverty_output,
)


def _collection(rows):
    return SimpleNamespace(dataframe=pd.DataFrame(rows))


def _row(poverty_type, group, rate, total_population):
    return {
        "poverty_type": poverty_type,
        "filter_group": group,
        "rate": rate,
        "total_population": total_population,
    }


def test_a_group_with_nobody_measured_has_no_rate():
    baseline = _collection(
        [
            _row("spm", "child", 0.12, 900.0),
            _row("spm", "senior", None, 0.0),
            _row("spm_deep", "senior", np.nan, 0.0),
        ]
    )
    reform = _collection(
        [
            _row("spm", "child", 0.10, 900.0),
            _row("spm", "senior", 0.0, 0.0),
        ]
    )
    output = build_poverty_output(
        country="us",
        baseline=None,
        reform=None,
        baseline_by_age=baseline,
        reform_by_age=reform,
    )
    assert (output.poverty.child.baseline, output.poverty.child.reform) == (0.12, 0.10)
    assert output.poverty.senior.baseline is None
    # A zero measured population is "nobody measured" even if a rate of 0 is
    # reported, as policyengine.py did before it distinguished the two.
    assert output.poverty.senior.reform is None
    assert output.deep_poverty.senior.baseline is None
    payload = output.model_dump(mode="json")
    assert payload["poverty"]["senior"] == {"baseline": None, "reform": None}


def test_a_measured_zero_rate_stays_zero():
    output = build_poverty_by_race_output(
        baseline_by_race=_collection([_row("spm", "other", 0.0, 50.0)]),
        reform_by_race=_collection([_row("spm", "other", 0.0, 50.0)]),
    )
    assert (output.poverty.other.baseline, output.poverty.other.reform) == (0.0, 0.0)


def test_groups_without_rows_keep_the_existing_default():
    output = build_poverty_by_race_output(baseline_by_race=None, reform_by_race=None)
    assert (output.poverty.white.baseline, output.poverty.white.reform) == (0.0, 0.0)

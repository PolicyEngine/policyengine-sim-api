"""Inequality output segment builders."""

from __future__ import annotations

from typing import Any

from policyengine_simulation_executor.simulation_macro_output import (
    BaselineReformValue,
    InequalityOutput,
)
from policyengine_simulation_executor.simulation_output_common import _number


def build_inequality(analysis: Any) -> InequalityOutput:
    baseline = getattr(analysis, "baseline_inequality", None)
    reform = getattr(analysis, "reform_inequality", None)
    if isinstance(baseline, InequalityOutput):
        return baseline
    return InequalityOutput(
        gini=BaselineReformValue(
            baseline=_number(getattr(baseline, "gini", None)),
            reform=_number(getattr(reform, "gini", None)),
        ),
        top_10_pct_share=BaselineReformValue(
            baseline=_number(getattr(baseline, "top_10_share", None)),
            reform=_number(getattr(reform, "top_10_share", None)),
        ),
        top_1_pct_share=BaselineReformValue(
            baseline=_number(getattr(baseline, "top_1_share", None)),
            reform=_number(getattr(reform, "top_1_share", None)),
        ),
    )

"""Stage 12 runtime INFO records reach the observability runtime's handler."""

from __future__ import annotations

import logging


def test_stage12_runtime_info_records_are_not_discarded_by_the_root_level() -> None:
    # The root logger stays at WARNING in a worker container, which would
    # discard INFO records, such as ``stage12_result_comparison_completed``,
    # before the observability runtime's handler sees them.
    import policyengine_simulation_executor.stage12_runtime  # noqa: F401  # the import sets the level

    # The package's own level, not a lower root level, enables them.
    assert (
        logging.getLogger("policyengine_simulation_executor.stage12_runtime").level
        == logging.INFO
    )
    assert logging.getLogger(
        "policyengine_simulation_executor.stage12_runtime.comparison"
    ).isEnabledFor(logging.INFO)

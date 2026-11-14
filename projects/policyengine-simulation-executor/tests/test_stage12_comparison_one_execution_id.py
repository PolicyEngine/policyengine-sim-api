"""A comparison-path report still needs two different execution IDs.

The contract accepts one simulation run on both sides of a report run, but a
comparison-path report keys its child records by execution ID, so it refuses
such an input before it claims or writes anything, as it did before.
"""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.stage12_execution import SimulationRole
from stage12_child_call_support import TimeoutInvoker
from stage12_fake_report_runs import economy_aggregate
from stage12_fake_stores import FakeArtifacts, FakeStore
from stage12_runtime_support import (
    coordinate_report,
    invocation_context,
    parent_record,
    report_execution_input,
)


def test_a_comparison_report_with_one_execution_id_is_refused_unwritten() -> None:
    report = report_execution_input()
    one_simulation = report.model_copy(
        update={
            "reform": report.baseline.model_copy(update={"role": SimulationRole.REFORM})
        }
    )
    store, invoker = FakeStore(), TimeoutInvoker()
    context = invocation_context()

    with pytest.raises(ValueError, match="simulation identifiers must differ"):
        coordinate_report(
            one_simulation.model_dump(mode="json"),
            context.model_dump(mode="json"),
            parent_record().model_dump(mode="json"),
            application_name=context.modal_application,
            coordinator_invocation_id="fc-coordinator",
            store=store,
            artifacts=FakeArtifacts(),
            invoker=invoker,
            aggregator=economy_aggregate,
            # Were it accepted, its children would never finish: close the
            # wait window at once rather than wait for them.
            wait_timeout_seconds=0,
            sleep=lambda _seconds: None,
        )

    assert store.parent is None
    assert store.children == {}
    assert invoker.events == []

"""Proof fixture: builder-test households from the weighted factory.

The rule must accept it.
"""

from __future__ import annotations

from types import SimpleNamespace

from fixtures.weighted_frames import weighted_entity_frame


def weighted_simulation(columns: dict[str, list[float]]) -> SimpleNamespace:
    """Build a simulation whose household output is weighted as policyengine's is."""
    household = weighted_entity_frame(columns)
    return SimpleNamespace(
        output_dataset=SimpleNamespace(data=SimpleNamespace(household=household))
    )

"""The shapes of a simulation's output data, as types code can check.

policyengine types ``Dataset.data`` as an optional ``BaseModel``, but each
country's year data holds weighted entity frames. Code that reads them checks
the shape first, for example::

    if not isinstance(simulation.output_dataset.data, EntityFrames):
        raise TypeError(...)
    frames = simulation.output_dataset.data.entity_data

Pyright then knows the type of that same expression. Reading through the
expression itself, rather than a helper's return value, keeps it one the
microdata weight checks recognise as a weighted frame.

The protocols are runtime-checkable, so the check only needs the attribute:
real year data and the tests' stand-ins both pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd


@runtime_checkable
class EntityFrames(Protocol):
    """Year data that holds each entity's weighted frame, by entity name."""

    @property
    def entity_data(self) -> Mapping[str, pd.DataFrame]:
        """Return each entity's weighted frame (a microdf MicroDataFrame)."""
        ...


@runtime_checkable
class HouseholdFrame(Protocol):
    """Year data with a weighted household frame."""

    @property
    def household(self) -> pd.DataFrame:
        """Return the weighted household frame (a microdf MicroDataFrame)."""
        ...

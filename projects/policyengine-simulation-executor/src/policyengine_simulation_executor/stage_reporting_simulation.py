"""A Simulation that reports when its calculation starts.

``economic_impact_analysis`` configures both simulations and then calls
``baseline.ensure()`` before ``reform.ensure()``. The runtime cannot observe
that boundary from outside the call, so each simulation reports the start of
its own first ``ensure()``. Later ``ensure()`` calls (the decile impacts call
it again) report nothing.
"""

from __future__ import annotations

from collections.abc import Callable

from policyengine.core import Simulation
from pydantic import PrivateAttr


class StageReportingSimulation(Simulation):
    _on_first_ensure: Callable[[], None] | None = PrivateAttr(default=None)

    def on_first_ensure(self, callback: Callable[[], None]) -> None:
        """Call ``callback`` once, when the first ``ensure()`` starts.

        ``callback`` must not raise: it runs inside the calculation.
        """

        self._on_first_ensure = callback

    def ensure(self) -> None:
        callback = self._on_first_ensure
        # Clear before the call so cached copies never carry the callback.
        self._on_first_ensure = None
        if callback is not None:
            callback()
        super().ensure()

"""Simulation doubles that answer the canonical SPM receipt contract.

On a canonical bundle ``_run_simulation_impl_core`` resolves a certified
selection for every US request and then reads ``spm_config`` and
``spm_provenance()`` off both simulations (``simulation_spm_result``). A
test whose subject is the macro output wiring still has to hand it objects
that answer, or it silently measures the pre-canonical no-selection arm
that a canonical bundle never reaches.

These doubles make no claim about the wrapper: they are the minimum shape
the executor reads. Wrapper conformance is the native lane's subject.
"""

from types import SimpleNamespace


def installed_spm_selection():
    """The selection a US request with no SPM settings resolves to, or None."""
    from policyengine_simulation_executor.spm import runtime_spm_capability

    capability = runtime_spm_capability()
    return None if capability is None else capability.defaults.model_dump()


def spm_receipt(selection, year="2026"):
    """One calculation receipt in the shape ``SPMProvenance`` accepts."""
    return {
        "forecast_id": "test-only",
        "forecast_sha256": selection["forecast_content_sha256"],
        "scenario": selection["scenario"],
        "geography_kind": selection["geography_kind"],
        "runtime_versions": {"policyengine-us": "test-only"},
        "years": {str(year): {"status": "forecast"}},
        "geographies": [],
        "composition_method": "classified-inputs",
        "storage_method": "formula",
    }


def spm_capable_simulation(selection, year="2026", **attributes):
    """A stand-in simulation that measured ``selection`` for ``year``."""
    if selection is None:
        return SimpleNamespace(**attributes)
    return SimpleNamespace(
        spm_config=dict(selection),
        spm_provenance=lambda: spm_receipt(selection, year),
        **attributes,
    )

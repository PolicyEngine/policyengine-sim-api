"""Shared stubs for ``collect_dataset_identity``'s input seams.

The key-discipline tests (test_artifact_keys) and the writer==reader
contract tests (test_precompute) must stub the SAME seam list —
``release_bundle.get_country_release_bundle``,
``release_bundle._receipt_dataset``,
``manifest.resolve_dataset_reference``, ``manifest.dataset_logical_name``,
``manifest.get_release_manifest``, ``spm.runtime_spm_capability`` — or one
suite silently tests a stale list when identity collection grows a seam.
This is that list's one home.

The SPM capability is a seam because ``collect_dataset_identity`` resolves
the installed bundle's certified selection into the dataset identity. Left
ambient it would put the installed bundle's forecast hash inside digests
that the same tests also derive directly from the synthetic version-set,
so the two derivations could never agree again and every golden would
churn on a data release rather than on a format change. The default is the
pre-canonical answer (no capability, no selection); a test that wants the
installed one assigns ``state.spm_capability =
state.installed_spm_capability()``.
"""

from types import SimpleNamespace


def install_identity_stubs(monkeypatch):
    """Stub the identity seams with the canonical synthetic version-set.

    Returns a mutable state (``bundle``, ``receipt_entry``,
    ``spm_capability``) so tests can vary any of them per case after
    installation, plus ``installed_spm_capability`` -- the real derivation,
    kept so a test can opt into the installed answer.
    """
    from policyengine.provenance import manifest as manifest_module

    # Binding order matters. ``get_release_manifest`` is stubbed on the
    # manifest module, but the wrapper's model-version module imports it by
    # value and calls it while constructing the country model. Import that
    # module first so it binds the real function: otherwise the first test
    # to trigger the import binds the stub into it for the rest of the
    # process, and an unrelated later test fails building the US model.
    import policyengine.tax_benefit_models.common.model_version  # noqa: F401

    from policyengine_simulation_executor import release_bundle

    bundle = SimpleNamespace(
        country="us",
        policyengine_version="4.22.0",
        model_version="9.9.9",
        data_version="1.2.3",
        data_artifact_revision="rev-abc",
        default_dataset="populace_cps",
        dataset_uris={
            "populace_cps": "hf://org/repo/populace_cps.h5@rev-abc",
        },
    )
    receipt_entry = {
        "country": "us",
        "version": "1.2.3",
        "installed_sha256": "feedbead" * 8,
    }
    state = SimpleNamespace(
        bundle=bundle, receipt_entry=receipt_entry, spm_capability=None
    )

    monkeypatch.setattr(
        release_bundle, "get_country_release_bundle", lambda country: state.bundle
    )
    monkeypatch.setattr(
        release_bundle, "_receipt_dataset", lambda country: state.receipt_entry
    )
    monkeypatch.setattr(
        manifest_module,
        "resolve_dataset_reference",
        lambda country, dataset: f"hf://org/repo/{dataset}.h5@rev-abc",
    )
    monkeypatch.setattr(
        manifest_module,
        "dataset_logical_name",
        lambda reference: state.bundle.default_dataset,
    )
    monkeypatch.setattr(
        manifest_module,
        "get_release_manifest",
        lambda country: SimpleNamespace(
            certification=SimpleNamespace(data_build_fingerprint="fp-123")
        ),
    )

    from policyengine_simulation_executor import spm as executor_spm

    # Keep a handle on the real derivation so a test can opt into the
    # installed answer without reaching around the stub.
    state.installed_spm_capability = executor_spm.runtime_spm_capability
    monkeypatch.setattr(
        executor_spm, "runtime_spm_capability", lambda: state.spm_capability
    )
    return state

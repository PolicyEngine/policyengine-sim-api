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
the installed bundle's SPM selection into the dataset identity. The default
stub has no capability so fixed identifier tests remain tied to their
synthetic inputs instead of whichever PolicyEngine release is installed.
"""

from types import SimpleNamespace


def install_identity_stubs(monkeypatch):
    """Stub the identity seams with the canonical synthetic version-set.

    Returns mutable bundle, receipt, and SPM-capability state, plus a handle
    to the installed capability resolver for tests that intentionally exercise
    the installed release.
    """
    from policyengine.provenance import manifest as manifest_module

    # This module imports ``get_release_manifest`` by value while constructing
    # country models. Import it before replacing the manifest module's function
    # so full-suite ordering cannot bind this test's stub into production code.
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
        dataset_data_versions={"populace_cps": "1.2.3"},
        dataset_revisions={"populace_cps": "rev-abc"},
        dataset_sha256s={"populace_cps": "feedbead" * 8},
        region_dataset_identities={"national": "populace_cps"},
    )
    receipt_entry = {
        "country": "us",
        "version": "1.2.3",
        "installed_sha256": "feedbead" * 8,
    }
    state = SimpleNamespace(
        bundle=bundle,
        receipt_entry=receipt_entry,
        spm_capability=None,
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

    state.installed_spm_capability = executor_spm.runtime_spm_capability
    monkeypatch.setattr(
        executor_spm,
        "runtime_spm_capability",
        lambda: state.spm_capability,
    )
    return state

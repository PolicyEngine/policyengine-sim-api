"""Shared stubs for ``collect_dataset_identity``'s input seams.

The key-discipline tests (test_artifact_keys) and the writer==reader
contract tests (test_precompute) must stub the SAME seam list —
``release_bundle.get_country_release_bundle``,
``release_bundle._receipt_dataset``,
``manifest.resolve_dataset_reference``, ``manifest.dataset_logical_name``,
``manifest.get_release_manifest`` — or one suite silently tests a stale
list when identity collection grows a seam. This is that list's one home.
"""

from types import SimpleNamespace


def install_identity_stubs(monkeypatch):
    """Stub the identity seams with the canonical synthetic version-set.

    Returns a mutable state (``bundle``, ``receipt_entry``) so tests can
    vary the receipt or bundle per case after installation.
    """
    from policyengine.provenance import manifest as manifest_module

    from policyengine_simulation_executor import release_bundle

    bundle = SimpleNamespace(
        country="us",
        policyengine_version="4.22.0",
        model_version="9.9.9",
        data_version="1.2.3",
        data_artifact_revision="rev-abc",
        default_dataset="populace_cps",
    )
    receipt_entry = {
        "country": "us",
        "version": "1.2.3",
        "installed_sha256": "feedbead" * 8,
    }
    state = SimpleNamespace(bundle=bundle, receipt_entry=receipt_entry)

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
    return state

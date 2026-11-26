"""The planned year file's name against the installed release.

The plan through the level handlers is tested in ``test_prebuilt_plan`` and
the handlers' targets in ``test_prebuilt_levels``; this one check needs the
real release manifest.
"""


def test_year_file_name_matches_runtime_stem_lookup(monkeypatch):
    """The national year file's name is the name the runtime looks for.

    Real-manifest coverage: it equals the exact name the runtime's
    ensure_datasets existence check resolves.
    """
    from policyengine.provenance.manifest import (
        dataset_logical_name,
        resolve_dataset_reference,
    )
    from policyengine_simulation_executor.artifact_keys import year_file_identity
    from policyengine_simulation_executor.release_bundle import (
        get_country_release_bundle,
        resolve_bundle_dataset_name,
    )
    from policyengine_simulation_executor.simulation_runtime import DEFAULT_YEAR
    from policyengine_simulation_executor.stage12_cache.levels_us import (
        national_year_file,
    )

    monkeypatch.delenv("POLICYENGINE_BUNDLE_RECEIPT", raising=False)
    get_country_release_bundle.cache_clear()
    try:
        expected_stem = dataset_logical_name(
            resolve_dataset_reference("us", resolve_bundle_dataset_name("us", None))
        )
        identity = year_file_identity(national_year_file(DEFAULT_YEAR))
    finally:
        get_country_release_bundle.cache_clear()
    assert identity.filename == f"{expected_stem}_year_{DEFAULT_YEAR}.h5"

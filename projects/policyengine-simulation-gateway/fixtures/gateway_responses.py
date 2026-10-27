"""Expected gateway response fields shared by the endpoint tests."""

from fixtures.gateway_endpoints import TEST_APP_RELEASE_BUNDLE, resolve_test_dataset_uri

OBSERVABILITY_ID = "00000000-0000-4000-8000-000000000001"
LOCAL_AREA_BREAKDOWN_RESULT = {
    "kind": "local_areas",
    "model_version": "2.132.6",
    "policyengine_version": "6.10.0",
    "dataset": "microcosm_uk_2024_25_dense",
    "data_version": "microcosm-uk-2024-25-dense-20261019T000432Z-ee5c2dbe",
    "constituency_impact": [
        {
            "constituency_code": "E14001063",
            "constituency_name": "Aldershot",
            "x": 56,
            "y": -40,
            "average_household_income_change": -150.0,
            "relative_household_income_change": -0.0026,
            "population": 40530.0,
            "disclosure_tier": "reported",
            "average_household_income_change_lower": -170.0,
            "average_household_income_change_upper": -130.0,
            "relative_household_income_change_lower": -0.003,
            "relative_household_income_change_upper": -0.0022,
        }
    ],
    "local_authority_impact": [],
}


def expected_bundle(
    country: str,
    model_version: str,
    *,
    dataset: str | None = None,
    data_version: str | None = None,
) -> dict[str, str | None]:
    """Return the policyengine_bundle a response reports for the test release bundle."""
    resolved_dataset = resolve_test_dataset_uri(country, dataset, data_version)
    if (
        data_version is not None
        and resolved_dataset is not None
        and resolved_dataset.startswith("hf://")
    ):
        resolved_dataset = (
            f"{resolved_dataset.rsplit('@', maxsplit=1)[0]}@{data_version}"
        )
    country_bundle = TEST_APP_RELEASE_BUNDLE[country]
    bundle: dict[str, str | None] = {
        "model_version": model_version,
        "policyengine_version": TEST_APP_RELEASE_BUNDLE["policyengine_version"],
        "data_version": data_version or country_bundle.get("data_version"),
        "dataset": resolved_dataset,
    }
    return {key: value for key, value in bundle.items() if value is not None}

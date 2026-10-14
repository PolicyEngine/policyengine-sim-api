"""Partial public selections inherit pinned bundle defaults before validation."""

import pytest
from policyengine_simulation_contract.gateway_models import SimulationRequest
from policyengine_simulation_contract.spm import (
    SPMInputError,
    SPMSelection,
    resolve_spm_selection,
)


def resolve(selection, *, kind="metro", area="35620"):
    return resolve_spm_selection(
        "us",
        selection,
        capability={
            "defaults": {
                "forecast_content_sha256": "a" * 64,
                "scenario": "ce_trend",
                "geography_kind": kind,
                "geography_id": area,
            },
        },
        policyengine_version="5.3.0",
        model_version="1.824.7",
    )


def test_partial_area_override_inherits_metro_default_through_request():
    request = SimulationRequest(country="us", spm={"geography_id": "31080"})
    payload = request.model_dump(mode="json")["spm"]
    assert payload == {"geography_id": "31080"}
    resolved = resolve(payload)
    assert resolved["geography_kind"] == "metro"
    assert resolved["geography_id"] == "31080"


@pytest.mark.parametrize("kind", ["county", "national"])
def test_partial_area_override_rejects_incompatible_defaults(kind):
    with pytest.raises(SPMInputError) as error:
        resolve({"geography_id": "31080"}, kind=kind, area=None)
    assert error.value.code == "SPM_SETTINGS_INVALID"


def test_explicit_metro_inherits_area_only_from_metro_defaults():
    assert resolve({"geography_kind": "metro"})["geography_id"] == "35620"
    with pytest.raises(SPMInputError) as error:
        resolve({"geography_kind": "metro"}, kind="county", area=None)
    assert error.value.code == "SPM_SETTINGS_INVALID"


def test_explicit_national_discards_default_metro_area():
    resolved = resolve({"geography_kind": "national"})
    assert resolved["geography_kind"] == "national"
    assert resolved["geography_id"] is None


@pytest.mark.parametrize(
    ("provenance", "wrapper", "model", "accepted"),
    [
        ("legacy-country-dict", None, "1.500.0", True),
        ("legacy-country-route", None, "1.715.2", True),
        ("legacy-country-route", None, "1.764.6", True),
        (None, None, "1.715.2", False),
        ("unknown", None, "1.715.2", False),
        ("legacy-seed", None, "1.715.2", False),
        ("legacy-country-route", None, "1.764.7", False),
        ("legacy-country-route", None, "unknown", False),
        ("legacy-country-dict", "5.3.1", "1.715.2", False),
    ],
)
def test_legacy_allowance_requires_route_provenance_and_historical_model(
    provenance, wrapper, model, accepted
):
    kwargs = {
        "capability": None,
        "policyengine_version": wrapper,
        "model_version": model,
        "route_provenance": provenance,
    }
    if accepted:
        assert resolve_spm_selection("us", None, **kwargs) is None
    else:
        with pytest.raises(SPMInputError) as error:
            resolve_spm_selection("us", None, **kwargs)
        assert error.value.code == "SPM_CONFIGURATION_UNAVAILABLE"
    for selection in ({}, {"geography_kind": "national"}):
        with pytest.raises(SPMInputError) as error:
            resolve_spm_selection("us", selection, **kwargs)
        assert error.value.code == "SPM_CONFIGURATION_UNAVAILABLE"


@pytest.mark.parametrize("wrapper", ["5.2.0", "5.3.0"])
@pytest.mark.parametrize("model", [None, "1.764.6"])
def test_pinned_pre_canonical_wrapper_is_historical_without_a_stated_model(
    wrapper, model
):
    """A routing entry with no bundle manifest states no model version.

    Requiring the pin's model version there rejected live 5.2.0/5.3.0
    routes, because the response's model version falls back to the wrapper
    version when the registry carries no manifest.
    """
    assert (
        resolve_spm_selection(
            "us",
            None,
            capability=None,
            policyengine_version=wrapper,
            model_version=model,
        )
        is None
    )


@pytest.mark.parametrize(
    ("wrapper", "model"),
    [
        ("5.2.0", "1.824.7"),
        ("5.3.0", "1.824.7"),
        ("5.4.0", None),
        ("5.4.0", "1.764.6"),
    ],
)
def test_unpinned_or_contradicted_wrapper_is_not_historical(wrapper, model):
    """Fail closed for wrappers that are not provably pre-canonical.

    A stated model version that contradicts the pin, and any wrapper outside
    the pinned pre-canonical set, still fail closed.
    """
    with pytest.raises(SPMInputError) as error:
        resolve_spm_selection(
            "us",
            None,
            capability=None,
            policyengine_version=wrapper,
            model_version=model,
        )
    assert error.value.code == "SPM_CONFIGURATION_UNAVAILABLE"


class TestSerializerPreservesExplicitNulls:
    """``exclude_none`` must not turn a resolved selection back into a partial.

    Presence is the contract: an omitted option inherits the bundle default,
    so an option that was explicitly selected as null is a different request
    from one that was omitted. The poll routes serialise every body with
    ``response_model_exclude_none``, so the serializer is the only thing
    standing between a completed result and a body a client cannot replay.
    """

    resolved = SPMSelection(
        forecast_content_sha256="a" * 64,
        scenario="ce_trend",
        geography_kind="national",
        geography_id=None,
        county_vintage="2020",
        as_of=None,
    )

    def test_exclude_none_keeps_explicitly_selected_nulls(self):
        assert self.resolved.model_dump(mode="json", exclude_none=True) == {
            "forecast_content_sha256": "a" * 64,
            "scenario": "ce_trend",
            "geography_kind": "national",
            "geography_id": None,
            "county_vintage": "2020",
            "as_of": None,
        }

    def test_exclude_none_still_drops_options_the_caller_omitted(self):
        partial = SPMSelection(geography_kind="national")
        assert partial.model_dump(mode="json", exclude_none=True) == {
            "geography_kind": "national"
        }

    def test_exclude_none_survives_nesting_in_a_response_model(self):
        nested = SimulationRequest(country="us", spm=self.resolved)
        assert nested.model_dump(mode="json", exclude_none=True)["spm"] == {
            "forecast_content_sha256": "a" * 64,
            "scenario": "ce_trend",
            "geography_kind": "national",
            "geography_id": None,
            "county_vintage": "2020",
            "as_of": None,
        }

    def test_explicit_exclude_still_removes_a_null_option(self):
        assert "as_of" not in self.resolved.model_dump(
            mode="json", exclude_none=True, exclude={"as_of"}
        )

    def test_ordinary_serialization_is_unchanged(self):
        assert self.resolved.model_dump(mode="json") == {
            "forecast_content_sha256": "a" * 64,
            "scenario": "ce_trend",
            "geography_kind": "national",
            "geography_id": None,
            "county_vintage": "2020",
            "as_of": None,
        }
        assert SPMSelection(geography_kind="national").model_dump(mode="json") == {
            "geography_kind": "national"
        }


@pytest.mark.parametrize("country", ["uk", "UK"])
def test_non_us_countries_take_no_spm_settings(country):
    assert (
        resolve_spm_selection(
            country,
            None,
            capability=None,
            policyengine_version="5.3.0",
            model_version="1.824.7",
        )
        is None
    )
    with pytest.raises(SPMInputError) as error:
        resolve_spm_selection(
            country,
            {},
            capability=None,
            policyengine_version="5.3.0",
            model_version="1.824.7",
        )
    assert error.value.code == "SPM_SETTINGS_INVALID"
    assert str(error.value) == "SPM settings are supported only for the US"


@pytest.mark.parametrize(
    ("wrapper", "historical"),
    [
        ("5.1.9", True),
        ("4.10.0", True),
        ("5.2.0", False),
        ("5.2.1", False),
        ("5.1", False),
        ("5.1.9.0", False),
        ("5.1.x", False),
        ("v5.1.9", False),
    ],
)
def test_wrappers_before_5_2_0_are_historical(wrapper, historical):
    """Three numeric parts below 5.2.0 are historical whatever the model says."""

    def resolve_unpinned():
        return resolve_spm_selection(
            "us",
            None,
            capability=None,
            policyengine_version=wrapper,
            model_version="9.9.9",
        )

    if historical:
        assert resolve_unpinned() is None
    else:
        with pytest.raises(SPMInputError) as error:
            resolve_unpinned()
        assert error.value.code == "SPM_CONFIGURATION_UNAVAILABLE"

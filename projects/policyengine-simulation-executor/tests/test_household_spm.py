"""Tests for household SPM selection and receipts, ported from the Public API."""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.spm import SPMInputError
from policyengine_simulation_executor import household_spm

FORECAST_SHA = "3d86d5c4c0423480e6b69b75d222ffa4a7a2639e4094df5ba2504af01be17173"
DEFAULTS = {
    "as_of": None,
    "county_vintage": "2020",
    "forecast_content_sha256": FORECAST_SHA,
    "geography_id": None,
    "geography_kind": "county",
    "scenario": "ce_trend",
}
RUNTIME_VERSIONS = {
    "policyengine": "6.7.5",
    "policyengine-core": "3.33.0",
    "policyengine-us": "2.2.1",
    "spm-calculator": "1.0.0",
}


class FakeForecast:
    years = (2025, 2026)

    def __init__(self, *, metro_areas=("31080",)):
        self.metro_areas = metro_areas

    def entry(self, year, *, scenario, as_of):
        if scenario not in {"ce_trend", "flat"}:
            msg = f"Unknown forecast scenario: {scenario}"
            raise ValueError(msg)

    def geography_factor(self, year, tenure, **options):
        if options["geoid"] not in self.metro_areas:
            msg = f"unknown area {options['geoid']}"
            raise ValueError(msg)
        return 1.0


@pytest.fixture
def certified_bundle(monkeypatch):
    monkeypatch.setattr(
        household_spm, "_bundle_spm_configuration", lambda: dict(DEFAULTS)
    )
    monkeypatch.setattr(household_spm, "_us_model_supports_spm", lambda: True)
    monkeypatch.setattr(household_spm, "certified_forecast", lambda sha: FakeForecast())


# Stands for a request that sends no SPM settings at all.
OMITTED = object()


def _resolve(selection=OMITTED, *, country="us", saved_household=False):
    return household_spm.resolve_household_spm_selection(
        country,
        None if selection is OMITTED else selection,
        sent=selection is not OMITTED,
        saved_household=saved_household,
    )


def _error(selection=OMITTED, **options) -> SPMInputError:
    with pytest.raises(SPMInputError) as raised:
        _resolve(selection, **options)
    return raised.value


def test_omitted_selection_resolves_the_certified_defaults(certified_bundle) -> None:
    assert _resolve() == DEFAULTS


def test_explicit_choices_override_defaults(certified_bundle) -> None:
    assert _resolve({"geography_kind": "metro", "geography_id": "31080"}) == {
        **DEFAULTS,
        "geography_kind": "metro",
        "geography_id": "31080",
    }
    assert _resolve({"scenario": "flat"})["scenario"] == "flat"


def test_other_countries_accept_no_selection(certified_bundle) -> None:
    assert _resolve(country="uk") is None
    error = _error({}, country="uk")
    assert (error.code, error.message) == (
        "SPM_SETTINGS_UNSUPPORTED",
        "SPM settings are only available for the US",
    )


def test_an_explicit_null_is_invalid_for_the_us_and_unsupported_elsewhere(
    certified_bundle,
) -> None:
    us_error = _error(None)
    uk_error = _error(None, country="uk")

    assert (us_error.code, us_error.message) == (
        "SPM_SETTINGS_INVALID",
        "spm must be an object; omit it to inherit the certified defaults.",
    )
    assert (uk_error.code, uk_error.message) == (
        "SPM_SETTINGS_UNSUPPORTED",
        "SPM settings are only available for the US",
    )


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        ({"geography_kind": "metro"}, "An SPM area selection requires geography_id"),
        ({"geography_id": "31080"}, "Only an SPM area selection accepts geography_id"),
        ("national", "Input should be a valid dictionary or instance of SPMSelection"),
    ],
)
def test_a_malformed_selection_is_invalid_before_the_bundle_is_read(
    monkeypatch, selection, message
) -> None:
    # As in the Public API, even a bundle without SPM support reports the
    # settings' own problem first.
    monkeypatch.setattr(household_spm, "_bundle_spm_configuration", lambda: None)
    monkeypatch.setattr(household_spm, "_us_model_supports_spm", lambda: False)

    error = _error(selection)

    assert (error.code, error.message) == ("SPM_SETTINGS_INVALID", message)


def test_a_foreign_artifact_hash_is_reported_by_where_it_came_from(
    certified_bundle,
) -> None:
    selection = {"forecast_content_sha256": "f" * 64}

    requested = _error(selection)
    saved = _error(selection, saved_household=True)

    assert (requested.code, requested.message) == (
        "SPM_SETTINGS_INVALID",
        "SPM selection does not match this bundle's artifact hash",
    )
    assert saved.code == "SPM_CONFIGURATION_UNAVAILABLE"
    assert "saved selection" in saved.message


@pytest.mark.parametrize(
    ("selection", "code", "message"),
    [
        (
            {"scenario": "unknown"},
            "SPM_SETTINGS_INVALID",
            "Unknown forecast scenario: unknown",
        ),
        (
            {"county_vintage": "2010"},
            "SPM_SETTINGS_INVALID",
            "Unsupported county vintage: use 2020",
        ),
        (
            {"geography_kind": "metro", "geography_id": "99999"},
            "SPM_GEOGRAPHY_UNAVAILABLE",
            "SPM area is unavailable in the selected artifact: 99999",
        ),
    ],
)
def test_invalid_selections_use_public_api_codes(
    certified_bundle, selection, code, message
) -> None:
    error = _error(selection)

    assert (error.code, error.message) == (code, message)


def test_a_bundle_without_spm_keeps_legacy_behaviour_only_for_old_models(
    monkeypatch,
) -> None:
    monkeypatch.setattr(household_spm, "_bundle_spm_configuration", lambda: None)
    monkeypatch.setattr(household_spm, "_us_model_supports_spm", lambda: False)

    assert _resolve() is None
    assert _error({}).code == "SPM_SETTINGS_UNSUPPORTED"

    monkeypatch.setattr(household_spm, "_us_model_supports_spm", lambda: True)
    assert _error().code == "SPM_CONFIGURATION_UNAVAILABLE"


def test_an_unreadable_bundle_manifest_fails_closed(monkeypatch) -> None:
    def unreadable_bundle():
        msg = "bundle manifest is missing"
        raise ValueError(msg)

    monkeypatch.setattr(household_spm, "_current_bundle", unreadable_bundle)

    error = _error()

    assert (error.code, error.message) == (
        "SPM_CONFIGURATION_UNAVAILABLE",
        "The installed bundle manifest is invalid",
    )


def test_changing_the_geography_kind_drops_an_inherited_area(monkeypatch) -> None:
    monkeypatch.setattr(
        household_spm,
        "_bundle_spm_configuration",
        lambda: {**DEFAULTS, "geography_kind": "metro", "geography_id": "31080"},
    )
    monkeypatch.setattr(household_spm, "_us_model_supports_spm", lambda: True)
    monkeypatch.setattr(household_spm, "certified_forecast", lambda sha: FakeForecast())

    assert _resolve({"geography_kind": "national"}) == {
        **DEFAULTS,
        "geography_kind": "national",
    }
    assert _resolve()["geography_id"] == "31080"


def test_an_uncertifiable_bundle_configuration_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        household_spm,
        "_bundle_spm_configuration",
        lambda: {**DEFAULTS, "scenario": None},
    )
    monkeypatch.setattr(household_spm, "_us_model_supports_spm", lambda: True)

    assert _error().code == "SPM_CONFIGURATION_UNAVAILABLE"


class FakeSimulation:
    def __init__(self, *, config=None, receipt=None):
        self.spm_config = dict(DEFAULTS) if config is None else config
        self.receipt = receipt

    def spm_provenance(self):
        return self.receipt


def _country_receipt(**overrides) -> dict:
    return {
        "forecast_id": "spm-forecast-2026",
        "forecast_sha256": FORECAST_SHA,
        "scenario": "ce_trend",
        "geography_kind": "county",
        "years": {"2027": {}, "2026": {}},
        "runtime_versions": RUNTIME_VERSIONS,
        "composition_method": "ignored by the compact receipt",
        **overrides,
    }


def test_the_country_receipt_becomes_the_compact_public_receipt() -> None:
    provenance = household_spm.household_spm_provenance(
        FakeSimulation(receipt=_country_receipt()), DEFAULTS
    )

    assert provenance is not None
    assert provenance.model_dump(mode="json", by_alias=True) == {
        "schema_version": "canonical-spm-provenance-v2",
        "forecast_id": "spm-forecast-2026",
        "forecast_sha256": FORECAST_SHA,
        "scenario": "ce_trend",
        "geography_kind": "county",
        "geography_id": None,
        "county_vintage": "2020",
        "as_of": None,
        "years": ["2026", "2027"],
        "runtime_versions": RUNTIME_VERSIONS,
    }


def test_a_country_without_spm_has_no_receipt() -> None:
    assert household_spm.household_spm_provenance(object(), None) is None


def test_an_uncertifiable_country_receipt_is_an_spm_error() -> None:
    with pytest.raises(SPMInputError) as raised:
        household_spm.household_spm_provenance(
            FakeSimulation(receipt=_country_receipt(scenario="flat")), DEFAULTS
        )

    assert raised.value.code == "SPM_CONFIGURATION_UNAVAILABLE"
    assert raised.value.message.startswith(
        "The installed country model reported an SPM receipt this API cannot certify"
    )


_UNCERTIFIABLE = (
    "The installed country model reported an SPM receipt this API cannot certify: "
)


def _receipt_without(*names: str, **overrides: object) -> dict:
    receipt = _country_receipt(**overrides)
    for name in names:
        del receipt[name]
    return receipt


@pytest.mark.parametrize(
    ("receipt", "reason"),
    [
        # Each required text is checked before the receipt is compared.
        (
            _receipt_without("scenario", forecast_sha256="b" * 64),
            "Country SPM receipt requires scenario",
        ),
        (
            _receipt_without("geography_kind", scenario="flat"),
            "Country SPM receipt requires geography_kind",
        ),
        (
            _country_receipt(years=["2026"]),
            "Country SPM receipt requires a years mapping",
        ),
        (
            _country_receipt(years={2026: {}}),
            "Country SPM receipt years must be strings",
        ),
        # Runtime versions are checked before the forecast ID.
        (
            _receipt_without("runtime_versions", "forecast_id"),
            "Country SPM receipt requires runtime_versions",
        ),
        (
            _receipt_without("forecast_id"),
            "Country SPM receipt requires forecast_id",
        ),
        (
            _country_receipt(years={"26": {}}),
            "years: years must contain four-digit calendar years",
        ),
    ],
)
def test_uncertifiable_receipts_are_worded_as_the_public_api_words_them(
    receipt, reason
) -> None:
    with pytest.raises(SPMInputError) as raised:
        household_spm.household_spm_provenance(
            FakeSimulation(receipt=receipt), DEFAULTS
        )

    assert raised.value.code == "SPM_CONFIGURATION_UNAVAILABLE"
    assert raised.value.message == _UNCERTIFIABLE + reason


def test_a_receipt_that_is_not_an_object_is_uncertifiable() -> None:
    with pytest.raises(SPMInputError) as raised:
        household_spm.household_spm_provenance(
            FakeSimulation(receipt=["not", "an", "object"]), DEFAULTS
        )

    assert (
        raised.value.message == _UNCERTIFIABLE + "Country SPM receipt must be an object"
    )


def test_a_receipt_for_another_selection_is_rejected() -> None:
    with pytest.raises(ValueError, match="differs from the request"):
        household_spm.household_spm_provenance(
            FakeSimulation(receipt=_country_receipt()),
            {**DEFAULTS, "county_vintage": "2010"},
        )
    with pytest.raises(ValueError, match="without SPM"):
        household_spm.household_spm_provenance(
            FakeSimulation(receipt=_country_receipt()), None
        )


class CountryInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def test_spm_errors_are_recognized_through_wrapping_exceptions() -> None:
    cause = CountryInputError("SPM_GEOGRAPHY_REQUIRED", "needs a county")
    wrapped = RuntimeError("while calculating spm_unit_spm_threshold")
    wrapped.__cause__ = cause

    detail = household_spm.household_spm_error_detail(wrapped)

    assert detail is not None
    assert (detail.code, detail.message) == ("SPM_GEOGRAPHY_REQUIRED", "needs a county")
    assert (
        household_spm.household_spm_error_detail(
            SPMInputError("SPM_SETTINGS_INVALID", "bad")
        )
        is not None
    )
    assert (
        household_spm.household_spm_error_detail(
            CountryInputError("SPM_SCENARIO_UNAVAILABLE", "no scenario")
        )
        is None
    )
    assert household_spm.household_spm_error_detail(ValueError("plain")) is None


def test_the_certified_forecast_is_loaded_once_by_its_digest() -> None:
    # The household path keeps its own loader, separate from the main path's
    # SPM module.
    forecast = household_spm.certified_forecast(FORECAST_SHA)

    assert household_spm.certified_forecast(FORECAST_SHA) is forecast
    assert forecast.years

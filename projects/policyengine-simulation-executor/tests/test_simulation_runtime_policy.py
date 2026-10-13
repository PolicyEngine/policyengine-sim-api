"""Policy compilation against the installed wrapper, without running a model."""

from datetime import datetime

import pytest
from policyengine.core import Dataset, Policy
from policyengine.tax_benefit_models.common import reform as wrapper_reform
from policyengine.utils.parametric_reforms import reform_dict_from_parameter_values
from policyengine_simulation_executor.simulation_runtime import (
    DEFAULT_YEAR,
    DatasetSelection,
    _build_simulation,
    _compile_interval_policy,
    _country_module,
    _normalise_policy,
    _parse_year,
)

UK_RATE = "gov.hmrc.income_tax.rates.uk[0].rate"
UK_ALLOWANCE = "gov.hmrc.income_tax.allowances.personal_allowance.amount"
US_SALT_CAP = "gov.irs.deductions.itemized.salt_and_real_estate.cap.JOINT"


def _naive_policy_date(year: int, month: int, day: int) -> datetime:
    """Return midnight on a date as a naive datetime.

    policyengine.py parses policy period keys with ``strptime`` and no zone, so
    its ``ParameterValue`` dates are naive; an aware value never compares equal.
    """
    return datetime(year, month, day)  # noqa: DTZ001  # naive like the dates under test


@pytest.fixture(scope="module")
def uk():
    country = _country_module("uk")
    assert UK_RATE in country.model.parameters_by_name
    assert UK_ALLOWANCE in country.model.parameters_by_name
    return country


def _parameter_value_contents(policy):
    """Compare effective values without wrapper-generated IDs and timestamps."""
    return [
        (
            value.parameter.name,
            value.parameter.tax_benefit_model_version.id,
            value.parameter.data_type,
            value.start_date,
            value.end_date,
            value.value,
        )
        for value in policy.parameter_values
    ]


@pytest.mark.parametrize("stop", ["2028-04-05", "2100-12-31", "2027-04-06"])
def test_uk_interval_policy_preserves_inclusive_stop_date(uk, stop):
    key = f"2027-04-06.{stop}"
    raw = {UK_RATE: {key: 0.21}}

    policy = _compile_interval_policy(
        _normalise_policy(raw), country_module=uk, year=2027
    )

    assert isinstance(policy, Policy)
    assert len(policy.parameter_values) == 1
    value = policy.parameter_values[0]
    assert value.parameter.name == UK_RATE
    assert value.parameter.tax_benefit_model_version is uk.model
    assert value.parameter.data_type is float
    assert value.start_date == _naive_policy_date(2027, 4, 6)
    assert value.end_date == datetime.strptime(stop, "%Y-%m-%d")  # noqa: DTZ007  # policyengine.py's ParameterValue dates are naive
    assert value.value == 0.21
    assert reform_dict_from_parameter_values(policy.parameter_values) == raw


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_effective_dates_and_intervals_are_chronological(uk, reverse):
    entries = [
        ("2026-04-06", 0.20),
        ("2027-04-06.2028-04-05", 0.21),
        ("2029-04-06", 0.22),
    ]
    if reverse:
        entries.reverse()

    policy = _compile_interval_policy(
        {UK_RATE: dict(entries)}, country_module=uk, year=2027
    )

    assert [
        (value.start_date, value.end_date, value.value)
        for value in policy.parameter_values
    ] == [
        (_naive_policy_date(2026, 4, 6), None, 0.20),
        (_naive_policy_date(2027, 4, 6), _naive_policy_date(2028, 4, 5), 0.21),
        (_naive_policy_date(2029, 4, 6), None, 0.22),
    ]
    assert reform_dict_from_parameter_values(policy.parameter_values) == {
        UK_RATE: {
            "2026-04-06.2027-04-05": 0.20,
            "2027-04-06.2028-04-05": 0.21,
            "2029-04-06.2100-12-31": 0.22,
        }
    }


def test_unknown_parameter_retains_wrapper_error_and_close_match_hint(uk):
    unknown = "gov.hmrc.income_tax.rates.uk[0].rat"
    raw = {unknown: {"2027-04-06.2028-04-05": 0.21}}

    with pytest.raises(ValueError) as expected:
        wrapper_reform.compile_reform(raw, year=2027, model_version=uk.model)
    with pytest.raises(ValueError) as actual:
        _compile_interval_policy(raw, country_module=uk, year=2027)

    assert str(actual.value) == str(expected.value)
    assert unknown in str(actual.value)
    assert f"did you mean '{UK_RATE}'" in str(actual.value)


@pytest.mark.parametrize(
    "key",
    [
        "2027",
        "2027-02-30",
        "2027-04-06.",
        ".2028-04-05",
        "2027-04-06.2028-04-05.extra",
        "2028-04-06.2027-04-05",
    ],
)
def test_invalid_policy_period_names_key_in_error(uk, key):
    raw = {UK_RATE: {"2026-04-06.2027-04-05": 0.20, key: 0.21}}

    with pytest.raises(ValueError) as normalise_error:
        _normalise_policy(raw)
    with pytest.raises(ValueError) as compile_error:
        _compile_interval_policy(raw, country_module=uk, year=2027)

    assert key in str(normalise_error.value)
    assert key in str(compile_error.value)


@pytest.mark.parametrize(
    "period,year",
    [
        ({"time_period": "2031", "year": "2030"}, 2031),
        ({"year": "2032"}, 2032),
        ({}, DEFAULT_YEAR),
    ],
)
def test_scalar_in_interval_policy_uses_request_year(uk, period, year):
    params = {"country": "uk", "scope": "macro", **period}
    raw = {
        UK_RATE: {"2027-04-06.2028-04-05": 0.21},
        UK_ALLOWANCE: 13000,
    }
    # No data is loaded. A different dataset year proves defaulting uses the
    # request's parsed year when the executor constructs the interval policy.
    dataset = Dataset(name="in-memory", description="Compilation only", year=2025)

    simulation = _build_simulation(
        params,
        dataset=dataset,
        dataset_selection=DatasetSelection("in-memory", "in-memory", False),
        policy=raw,
        region_code="uk",
    )

    assert _parse_year(params) == year
    scalar = next(
        value
        for value in simulation.policy.parameter_values
        if value.parameter.name == UK_ALLOWANCE
    )
    assert scalar.start_date == _naive_policy_date(year, 1, 1)
    assert scalar.end_date is None
    assert scalar.value == 13000
    assert scalar.parameter.data_type is int
    assert reform_dict_from_parameter_values(simulation.policy.parameter_values) == {
        UK_RATE: {"2027-04-06.2028-04-05": 0.21},
        UK_ALLOWANCE: {f"{year}-01-01.2100-12-31": 13000},
    }


def test_us_interval_policy_uses_same_public_parameter_values():
    us = _country_module("us")
    assert US_SALT_CAP in us.model.parameters_by_name
    raw = {US_SALT_CAP: {"2027-01-01.2027-12-31": 50000}}

    policy = _compile_interval_policy(raw, country_module=us, year=2027)

    assert isinstance(policy, Policy)
    assert len(policy.parameter_values) == 1
    value = policy.parameter_values[0]
    assert value.parameter.name == US_SALT_CAP
    assert value.parameter.tax_benefit_model_version is us.model
    assert value.parameter.data_type is int
    assert value.start_date == _naive_policy_date(2027, 1, 1)
    assert value.end_date == _naive_policy_date(2027, 12, 31)
    assert value.value == 50000
    assert reform_dict_from_parameter_values(policy.parameter_values) == raw


@pytest.mark.parametrize(
    "spec",
    [0.21, 1, False, "0.21", {"2027-04-06": 0.21, "2028-04-06": 0.22}],
)
def test_policy_without_intervals_keeps_original_wrapper_compilation(
    uk, monkeypatch, spec
):
    raw = {UK_RATE: spec}
    original_compiler = wrapper_reform.compile_reform_to_policy
    expected = original_compiler(raw, year=2027, model_version=uk.model)
    calls = []

    def recording_compiler(reform, *, year, model_version):
        calls.append((reform, year, model_version))
        return original_compiler(reform, year=year, model_version=model_version)

    monkeypatch.setattr(wrapper_reform, "compile_reform_to_policy", recording_compiler)
    assert _compile_interval_policy(raw, country_module=uk, year=2027) is raw

    simulation = _build_simulation(
        {"country": "uk", "scope": "macro", "time_period": "2027"},
        dataset=Dataset(name="in-memory", description="Compilation only", year=2027),
        dataset_selection=DatasetSelection("in-memory", "in-memory", False),
        policy=raw,
        region_code="uk",
    )

    assert len(calls) == 1
    # The wrapper's Simulation validator receives a copy of the dict.
    assert calls[0][0] == raw
    assert calls[0][1] == 2027
    assert calls[0][2] is uk.model
    assert _parameter_value_contents(simulation.policy) == _parameter_value_contents(
        expected
    )
    assert reform_dict_from_parameter_values(
        simulation.policy.parameter_values
    ) == reform_dict_from_parameter_values(expected.parameter_values)


@pytest.mark.parametrize(
    "key, start, stop",
    [
        ("2027-4-6", _naive_policy_date(2027, 4, 6), None),
        (
            "2027-4-6.2028-4-5",
            _naive_policy_date(2027, 4, 6),
            _naive_policy_date(2028, 4, 5),
        ),
    ],
)
def test_unpadded_dates_are_accepted_as_the_wrapper_always_accepted_them(
    key, start, stop
):
    from policyengine_simulation_executor.simulation_runtime import (
        _parse_policy_period,
    )

    assert _normalise_policy({UK_RATE: {key: 0.21}}) == {UK_RATE: {key: 0.21}}
    assert _parse_policy_period(key) == (start, stop)

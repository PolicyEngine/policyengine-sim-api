"""How .py turns parameter values into the reform values a simulation takes.

The simulation service's policy reader (``policy_reform`` in the simulation
persistence library, which this environment doesn't install) follows .py's
``reform_dict_from_parameter_values`` for values without an end date. These
tests pin .py's periods for the cases the reader's tests check; the reader
gives the same periods, except that its last open-ended value runs to the end
of 2200, the latest year a calculation can be for, where .py's runs to 2100.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from policyengine.core import ParameterValue
from policyengine.utils.parametric_reforms import (
    OPEN_ENDED_REFORM_STOP,
    reform_dict_from_parameter_values,
)
from policyengine_core.entities import build_entity
from policyengine_core.parameters import ParameterNode
from policyengine_core.reforms import Reform
from policyengine_core.taxbenefitsystems import TaxBenefitSystem

CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
STANDARD_DEDUCTION = "gov.irs.deductions.standard.amount.SINGLE"


def _value(
    parameter_name: str,
    value: float,
    start_date: datetime,
    end_date: datetime | None = None,
) -> ParameterValue:
    """Return a parameter value; .py reads only its parameter's name here."""
    return ParameterValue.model_construct(
        parameter=SimpleNamespace(name=parameter_name),
        value=value,
        start_date=start_date,
        end_date=end_date,
    )


def test_py_gives_way_to_a_later_bounded_value_for_good() -> None:
    """After the bounded value ends, current law applies."""
    reform = reform_dict_from_parameter_values(
        [
            _value(CTC_AMOUNT, 3_000, datetime(2026, 1, 1, tzinfo=UTC)),
            _value(
                CTC_AMOUNT,
                5_000,
                datetime(2028, 1, 1, tzinfo=UTC),
                datetime(2028, 12, 31, tzinfo=UTC),
            ),
        ]
    )

    assert reform == {
        CTC_AMOUNT: {
            "2026-01-01.2027-12-31": 3_000,
            "2028-01-01.2028-12-31": 5_000,
        }
    }


def test_py_runs_open_ended_values_one_after_another() -> None:
    reform = reform_dict_from_parameter_values(
        [
            _value(CTC_AMOUNT, 1_000, datetime(2026, 1, 1, tzinfo=UTC)),
            _value(CTC_AMOUNT, 2_000, datetime(2027, 7, 1, tzinfo=UTC)),
            _value(CTC_AMOUNT, 3_000, datetime(2030, 1, 1, tzinfo=UTC)),
            _value(STANDARD_DEDUCTION, 20_000, datetime(2029, 1, 1, tzinfo=UTC)),
        ]
    )

    assert OPEN_ENDED_REFORM_STOP == "2100-12-31"
    assert reform == {
        CTC_AMOUNT: {
            "2026-01-01.2027-06-30": 1_000,
            "2027-07-01.2029-12-31": 2_000,
            "2030-01-01.2100-12-31": 3_000,
        },
        STANDARD_DEDUCTION: {"2029-01-01.2100-12-31": 20_000},
    }


def test_py_lets_the_later_of_two_values_starting_the_same_day_take_over() -> None:
    """.py ends the earlier value before it starts, so only the later applies.

    The reform is applied with policyengine-core to a one-parameter system.
    """
    reform = reform_dict_from_parameter_values(
        [
            _value("credit.amount", 3_000, datetime(2026, 1, 1, tzinfo=UTC)),
            _value("credit.amount", 4_000, datetime(2026, 1, 1, 12, tzinfo=UTC)),
        ]
    )
    person = build_entity(key="person", plural="people", label="Person", is_person=True)
    system = TaxBenefitSystem(entities=[person])
    system.parameters = ParameterNode(
        "", data={"credit": {"amount": {"values": {"2020-01-01": 1_000}}}}
    )

    amount = Reform.from_dict(reform)(system).parameters.get_child("credit.amount")

    assert reform == {
        "credit.amount": {
            "2026-01-01.2025-12-31": 3_000,
            "2026-01-01.2100-12-31": 4_000,
        }
    }
    assert [
        amount(instant) for instant in ("2025-12-31", "2026-01-01", "2099-06-01")
    ] == [
        1_000,
        4_000,
        4_000,
    ]

"""The API route of the UK Budget reform contract.

The contract (policyengine.py's
``policyengine.tax_benefit_models.uk.budget_reform_contract``) checks its
cases through ``Simulation(policy=...)`` in-process and leaves the API route
to this service. A request reaches that same wrapper route after policy
normalisation, so the API route holds when normalisation hands the wrapper
the same parameter values for every case: legacy ``start.stop`` keys in,
identical effective dates and end dates out. This test runs once the
installed wrapper ships the contract.
"""

import pytest

contract = pytest.importorskip(
    "policyengine.tax_benefit_models.uk.budget_reform_contract"
)

from policyengine.tax_benefit_models.common.reform import (  # noqa: E402
    compile_reform_to_policy,
)
from policyengine.utils.parametric_reforms import (  # noqa: E402
    reform_dict_from_parameter_values,
)

from policyengine_simulation_executor.simulation_runtime import (  # noqa: E402
    _compile_interval_policy,
    _country_module,
    _normalise_policy,
)

CASES = [
    case
    for case in contract.CASES
    if case.changes and (case.routes is None or "wrapper.policy" in case.routes)
]


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_api_hands_the_wrapper_each_case_unchanged(case):
    uk = _country_module("uk")
    year = min(check.year for check in case.checks) if case.checks else 2027
    # The app and gateway send the legacy start.stop form.
    request = contract.country_reform_dict(case.changes)
    wrapper_form = contract.represent(case, "wrapper.policy")
    if case.expect_error is not None:
        with pytest.raises(ValueError, match=case.expect_error):
            _compile_interval_policy(
                _normalise_policy(request), country_module=uk, year=year
            )
        with pytest.raises(ValueError, match=case.expect_error):
            compile_reform_to_policy(wrapper_form, year=year, model_version=uk.model)
        return
    through_api = _compile_interval_policy(
        _normalise_policy(request), country_module=uk, year=year
    )
    through_wrapper = compile_reform_to_policy(
        wrapper_form, year=year, model_version=uk.model
    )
    assert reform_dict_from_parameter_values(
        through_api.parameter_values
    ) == reform_dict_from_parameter_values(through_wrapper.parameter_values)

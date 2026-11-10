"""How the household path names the job's policy in the household contract.

``policyengine_simulation_contract.household_execution`` defines the request
a household function takes. Everything the service depends on about how
that request names the policy a job calculates is here, so a change to the
contract's policy fields changes this module alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from policyengine_simulation_persistence import PolicyReform

# The name the request gives the job's one policy.
CURRENT_LAW_POLICY_KEY: Final = "baseline"
STORED_POLICY_POLICY_KEY: Final = "reform"
# The request's top-level fields that hold the policy.
POLICY_FIELDS: Final = frozenset({"policies"})


def job_policy_fields(*, current_law: bool, reform: PolicyReform) -> dict[str, object]:
    """Return the request fields naming the job's policy and its reform values.

    Current law changes no parameter.
    """
    policy_key = CURRENT_LAW_POLICY_KEY if current_law else STORED_POLICY_POLICY_KEY
    return {"policies": [{"policy_key": policy_key, "parameters": reform}]}


def names_the_policy(location: tuple[str | int, ...]) -> bool:
    """Return whether a refused field of the request belongs to its policy."""
    return bool(location) and location[0] in POLICY_FIELDS

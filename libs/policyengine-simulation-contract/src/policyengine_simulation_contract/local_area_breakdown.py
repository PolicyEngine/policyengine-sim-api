"""The worker capability that serves UK local-area breakdown requests.

A ``SimulationRequest`` with ``breakdown: "local_areas"`` runs only the
constituency and local-authority impacts on the UK local-area dataset, and
returns a ``LocalAreaBreakdownOutput``. Worker apps built before this existed
ignore the field and would return a national result, so the routing bundle
manifest of each worker that serves it declares
``LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION`` under
``LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY``, and the gateway refuses the field for
any other worker.
"""

from __future__ import annotations

# The one value of ``SimulationRequest.breakdown``.
LOCAL_AREAS_BREAKDOWN = "local_areas"
LOCAL_AREA_BREAKDOWN_CAPABILITY_KEY = "local_area_breakdown"
LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION = 1
# The nation regions policyengine.py registers for the UK (its UK_COUNTRIES);
# an executor test checks this against the installed registry, since the
# gateway installs no country package.
UK_NATION_REGIONS = frozenset(
    {
        "country/england",
        "country/scotland",
        "country/wales",
        "country/northern_ireland",
    }
)


def supported_local_area_breakdown_capability(value: object) -> int | None:
    """Return the declared capability version when this gateway reads it."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value != LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION:
        return None
    return value


def local_area_breakdown_error(
    *,
    country: str,
    region: str | None,
    region_group: object,
    include_cliffs: object,
    segmented: object,
) -> str | None:
    """Return why a request cannot have a local-area breakdown, or None.

    A breakdown runs one UK-wide or nation region on the local-area dataset,
    so it needs a UK request whose region is absent, ``uk`` or one of
    ``UK_NATION_REGIONS``, in any case. It computes no cliff impacts and no region
    segments, and takes no region group. ``include_cliffs`` and ``segmented``
    may still be sent as false: the Public API always sends
    ``include_cliffs: false``. The gateway contract and the worker both apply
    these rules.
    """
    if country.lower() != "uk":
        return "breakdown is supported only for UK requests"
    if region is not None and region.lower() not in {"uk", *UK_NATION_REGIONS}:
        accepted = ", ".join(["uk", *sorted(UK_NATION_REGIONS)])
        return (
            f"breakdown needs a UK-wide or nation region ({accepted}), not {region!r}"
        )
    for flag, value in (
        ("include_cliffs", include_cliffs),
        ("segmented", segmented),
    ):
        if value is True:
            return f"breakdown cannot be combined with {flag}=true"
    if region_group is not None:
        return "breakdown cannot be combined with region_group"
    return None

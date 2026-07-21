"""Static region-group partition for segmented national runs.

A plain US national macro request runs as one simulation per group below
(each a ``region_group`` request over whole states), and a coordinator
reduces the groups' microdata back into the national output. Partitioning
by whole states with row-filter scoping preserves national weights, so the
reduce is exact — see the segmented-national plan.

Provenance: household-balanced 20-way partition computed from the actual
dataset household counts (LPT greedy over per-state rows; no state needed
splitting into districts). Groups hold 2,440-3,042 of the 57,240 sample
households each. Rebalance only with a measured partition; group count and
membership changes alter per-child runtimes, not correctness.
"""

US_NATIONAL_REGION_GROUPS: tuple[tuple[str, ...], ...] = (
    ("state/hi", "state/ia", "state/wi"),
    ("state/ak", "state/ga", "state/la"),
    ("state/al", "state/co", "state/ri"),
    ("state/mn", "state/mo", "state/nh"),
    ("state/az", "state/de", "state/me"),
    ("state/ny", "state/wv"),
    ("state/in", "state/ky", "state/md"),
    ("state/mt", "state/sc", "state/wa"),
    ("state/id", "state/or", "state/tn"),
    ("state/ar", "state/nc", "state/nd"),
    ("state/dc", "state/ks", "state/mi"),
    ("state/ct", "state/oh", "state/vt"),
    ("state/nv", "state/va", "state/wy"),
    ("state/pa", "state/ut"),
    ("state/sd", "state/tx"),
    ("state/ca",),
    ("state/il", "state/ms"),
    ("state/fl", "state/nm"),
    ("state/ne", "state/nj"),
    ("state/ma", "state/ok"),
)


def national_region_groups(country: str) -> list[list[str]] | None:
    """The region-group partition for a country's segmented national run.

    Returns None when the country has no partition (only the US has one),
    in which case national requests run monolithically.
    """
    if country.lower() == "us":
        return [list(group) for group in US_NATIONAL_REGION_GROUPS]
    return None

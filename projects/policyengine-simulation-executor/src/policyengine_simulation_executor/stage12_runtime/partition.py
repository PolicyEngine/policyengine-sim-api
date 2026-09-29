"""Stage 12-owned US partition for segmented national calculations."""

from __future__ import annotations

from typing import Any

# This is an independent snapshot of the measured 20-way partition used by
# the v1 runner when Stage 12 segmentation was introduced. Stage 12 must not
# import the v1 partition: either execution path can now evolve without
# changing the other one implicitly.
US_REGION_GROUPS: tuple[tuple[str, ...], ...] = (
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


def stage12_region_groups(country: str) -> list[list[str]] | None:
    """Return a mutable copy of the Stage 12 partition when supported."""

    if country.lower() != "us":
        return None
    return [list(group) for group in US_REGION_GROUPS]


def stage12_region_groups_for_model(country: str, model: Any) -> list[list[str]]:
    """Return the partition, retaining newly registered state regions.

    The static partition is measured for the certified dataset. A later model
    can register another state-level region before the partition is rebalanced;
    assigning it to the final group prevents silently omitting its households.
    """

    groups = stage12_region_groups(country)
    if groups is None:
        raise ValueError(f"Stage 12 has no national partition for {country!r}")
    registry = getattr(model, "region_registry", None)
    if registry is None:
        return groups
    registered = {
        region.code
        for region in registry.regions
        if getattr(region, "region_type", None) == "state"
    }
    covered = {code for group in groups for code in group}
    groups[-1].extend(sorted(registered - covered))
    return groups

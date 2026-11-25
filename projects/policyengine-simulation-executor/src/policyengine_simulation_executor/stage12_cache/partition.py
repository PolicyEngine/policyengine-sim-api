"""The household-balanced 20-way US partition for segmented national calculations.

One list for both apps: Stage 12 segments national runs by it
(``stage12_runtime.partition``), the run_simulation app's segmented national
runs use it (``national_partition``), and the US level handlers precompute one
current-law baseline per group (``levels_us``). It lives here, rather than in
``stage12_runtime``, because that package's ``__init__`` imports the v2
database code, which the run_simulation app's images don't install.

Provenance: computed from the dataset's household counts per state (LPT
greedy over per-state rows; no state needed splitting into districts).
Groups hold 2,440-3,042 of the 57,240 sample households each. Rebalance only
with a measured partition; group count and membership changes alter
per-child runtimes and every precomputed group baseline's key, not
correctness.
"""

from __future__ import annotations

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

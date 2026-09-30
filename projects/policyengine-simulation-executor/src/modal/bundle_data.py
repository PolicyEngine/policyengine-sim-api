"""Build the dataset-only PolicyEngine bundle command used by Modal images."""

from __future__ import annotations

import shlex
from collections.abc import Sequence


def bundle_data_install_command(
    policyengine_version: str,
    *,
    countries: Sequence[str],
    data_dir: str,
) -> str:
    """Download certified datasets without changing Python packages."""
    parts = [
        "policyengine",
        "bundle",
        "install",
        policyengine_version,
        "--no-packages",
    ]
    for country in countries:
        parts.extend(("--country", country))
    parts.extend(("--data-dir", data_dir, "--yes"))
    return " ".join(shlex.quote(part) for part in parts)

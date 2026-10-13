"""Identifiers and hashes that correlate observability records across services."""

from __future__ import annotations

import json
from hashlib import sha256
from uuid import uuid4


def generate_observability_id() -> str:
    """Return a new random UUID4 string identifying one run across services."""
    return str(uuid4())


def stable_config_hash(payload: object) -> str:
    """Return a ``sha256:``-prefixed digest of the payload's canonical JSON.

    Keys are sorted and non-JSON values are stringified, so equal configurations
    hash identically regardless of key order.
    """
    serialised = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{sha256(serialised.encode()).hexdigest()}"

"""Canonical JSON hashing and the clock shared by the job and report stores."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime


def canonical_json(value: object) -> bytes:
    """Encode ``value`` as JSON with sorted keys and no whitespace."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def result_sha256(result: object) -> str:
    """Return the SHA-256 of a result as canonical JSON."""
    return hashlib.sha256(canonical_json(result)).hexdigest()


def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)

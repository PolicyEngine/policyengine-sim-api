from __future__ import annotations

from hashlib import sha256
import json
from typing import Any
from uuid import uuid4


def generate_observability_id() -> str:
    return str(uuid4())


def stable_config_hash(payload: Any) -> str:
    serialised = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{sha256(serialised.encode()).hexdigest()}"

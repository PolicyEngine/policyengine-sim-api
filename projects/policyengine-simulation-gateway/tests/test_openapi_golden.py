"""The generated OpenAPI spec must match the checked-in golden.

The golden was captured from main immediately before the gateway moved
out of the executor project, so this pins the public contract across the
migration. Intentional API changes update the golden explicitly:

    uv run python -m policyengine_simulation_gateway.generate_openapi
    cp artifacts/openapi.json tests/golden/openapi.json
"""

import json
from pathlib import Path

from policyengine_simulation_gateway.generate_openapi import create_openapi_app

GOLDEN = Path(__file__).parent / "golden" / "openapi.json"


def test_openapi_spec_matches_golden():
    generated = create_openapi_app().openapi()
    golden = json.loads(GOLDEN.read_text())
    assert generated == golden, (
        "Generated OpenAPI spec differs from tests/golden/openapi.json. "
        "If the API change is intentional, regenerate the golden and "
        "review the diff."
    )

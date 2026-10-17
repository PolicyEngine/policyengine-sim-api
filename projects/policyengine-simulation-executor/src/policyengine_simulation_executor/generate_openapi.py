"""Write the executor app's OpenAPI spec to ``artifacts/openapi.json`` when imported."""

import json
from pathlib import Path

from .main import app

# Write OpenAPI spec directly to file
output_path = Path(__file__).parent.parent.parent / "artifacts" / "openapi.json"
output_path.parent.mkdir(parents=True, exist_ok=True)

openapi_spec = app.openapi()
with output_path.open("w") as f:
    json.dump(openapi_spec, f, indent=4)

print(f"OpenAPI spec written to {output_path}")  # noqa: T201  # command-line script: reports where it wrote the spec

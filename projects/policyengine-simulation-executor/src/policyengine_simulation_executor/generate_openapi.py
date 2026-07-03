from .main import app
import json
from pathlib import Path

# Write OpenAPI spec directly to file
output_path = Path(__file__).parent.parent.parent / "artifacts" / "openapi.json"
output_path.parent.mkdir(parents=True, exist_ok=True)

openapi_spec = app.openapi()
with open(output_path, "w") as f:
    json.dump(openapi_spec, f, indent=4)

print(f"OpenAPI spec written to {output_path}")

"""Generate the Cloud Run Simulation Entrypoint OpenAPI document."""

from __future__ import annotations

import json
from pathlib import Path

from policyengine_simulation_entry.app import create_app


def main() -> None:
    """Write the entry app's OpenAPI spec to ``artifacts/openapi.json``.

    ``scripts/generate-clients.sh`` builds the published simulation client
    from this file.
    """
    output = Path(__file__).resolve().parents[2] / "artifacts" / "openapi.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(create_app().openapi(), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"OpenAPI spec written to {output}")  # noqa: T201  # command-line script: reports where it wrote the spec


if __name__ == "__main__":
    main()

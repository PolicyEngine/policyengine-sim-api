"""Live Modal check for the UK private-data Hugging Face credential.

This check runs immediately after secret synchronization and before the
simulation image build. It therefore validates the stored Modal value even
when the dataset installation layer is already cached.
"""

from __future__ import annotations

import json
from pathlib import Path

import modal

from policyengine_simulation_executor.hf_access_validation import (
    HF_MODAL_SECRET_NAME,
    HF_RUNTIME_ENV_NAME,
    HFAccessTarget,
    build_uk_bundle_access_plan,
)

app = modal.App("pe-uk-private-hf-read-access-check")
hf_secret = modal.Secret.from_name(
    HF_MODAL_SECRET_NAME,
    required_keys=[HF_RUNTIME_ENV_NAME],
)

_UV_PROJECT_DIR = str(Path(__file__).resolve().parents[2]) if modal.is_local() else "."
image = (
    modal.Image.debian_slim(python_version="3.13")
    .uv_sync(
        uv_project_dir=_UV_PROJECT_DIR,
        frozen=True,
        extra_options="--only-group modal-simulation-image",
    )
    .add_local_python_source(
        "policyengine_simulation_executor",
        "policyengine_simulation_contract",
        copy=True,
    )
)


@app.function(image=image, secrets=[hf_secret], timeout=120)
def verify_access(target_payloads: list[dict[str, str]]) -> dict[str, object]:
    from policyengine_simulation_executor.hf_access_validation import (
        validate_uk_private_hf_access,
    )

    targets = tuple(
        HFAccessTarget.model_validate(payload) for payload in target_payloads
    )
    return validate_uk_private_hf_access(targets).model_dump(mode="json")


@app.local_entrypoint()
def main() -> None:
    targets = build_uk_bundle_access_plan()
    payloads = [target.model_dump(mode="json") for target in targets]
    result = verify_access.remote(payloads)
    print(json.dumps(result, sort_keys=True))

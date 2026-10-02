"""Tests for the UK private-data Hugging Face credential contract."""

from __future__ import annotations

from hashlib import sha256

import pytest

from policyengine_simulation_executor.hf_access_validation import (
    EXPECTED_HF_TOKEN_DISPLAY_NAME,
    HF_RUNTIME_ENV_NAME,
    HFArtifactAccess,
    HFAccessTarget,
    build_uk_bundle_access_plan,
    validate_uk_private_hf_access,
)


def _identity(*, permissions: tuple[str, ...] = ("repo.content.read",)) -> dict:
    return {
        "name": "policyengine-runtime-data-reader",
        "auth": {
            "accessToken": {
                "displayName": EXPECTED_HF_TOKEN_DISPLAY_NAME,
                "role": "fineGrained",
                "fineGrained": {
                    "global": [],
                    "scoped": [{"permissions": list(permissions)}],
                },
            }
        },
    }


def _target() -> HFAccessTarget:
    return HFAccessTarget(
        repo_id="policyengine/policyengine-uk-data-private",
        repo_type="model",
        path="enhanced_frs_2024_25.h5",
        revision="1.56.16",
    )


def test_bundle_access_plan_covers_both_private_repositories() -> None:
    targets = build_uk_bundle_access_plan()

    assert {target.repo_id for target in targets} == {
        "policyengine/policyengine-uk-data-private",
        "policyengine/populace-uk-private",
    }
    assert all(target.revision for target in targets)
    assert all(target.path for target in targets)
    assert not any("staging" in target.repo_id for target in targets)


def test_validation_returns_only_safe_identity_and_artifact_metadata() -> None:
    token = "private-test-token"
    target = _target()

    result = validate_uk_private_hf_access(
        (target,),
        environment={HF_RUNTIME_ENV_NAME: token},
        whoami_loader=lambda _: _identity(),
        metadata_loader=lambda checked, _: HFArtifactAccess(
            target=checked,
            commit_hash="commit",
            etag="etag",
            size=123,
        ),
    )

    assert result.account == "policyengine-runtime-data-reader"
    assert result.token_display_name == EXPECTED_HF_TOKEN_DISPLAY_NAME
    assert result.token_fingerprint == sha256(token.encode()).hexdigest()[:12]
    assert result.artifacts[0].target == target
    assert token not in result.model_dump_json()


def test_validation_rejects_unexpected_token_identity() -> None:
    identity = _identity()
    identity["auth"]["accessToken"]["displayName"] = "ambiguous-old-token"

    with pytest.raises(RuntimeError, match="Unexpected Hugging Face token identity"):
        validate_uk_private_hf_access(
            (_target(),),
            environment={HF_RUNTIME_ENV_NAME: "token"},
            whoami_loader=lambda _: identity,
            metadata_loader=lambda *_: pytest.fail("metadata must not be requested"),
        )


def test_validation_rejects_write_permission() -> None:
    with pytest.raises(RuntimeError, match="must not have write permission"):
        validate_uk_private_hf_access(
            (_target(),),
            environment={HF_RUNTIME_ENV_NAME: "token"},
            whoami_loader=lambda _: _identity(
                permissions=("repo.content.read", "repo.write")
            ),
            metadata_loader=lambda *_: pytest.fail("metadata must not be requested"),
        )


def test_validation_rejects_missing_runtime_credential() -> None:
    with pytest.raises(RuntimeError, match=HF_RUNTIME_ENV_NAME):
        validate_uk_private_hf_access(
            (_target(),),
            environment={},
            whoami_loader=lambda _: pytest.fail("identity must not be requested"),
            metadata_loader=lambda *_: pytest.fail("metadata must not be requested"),
        )

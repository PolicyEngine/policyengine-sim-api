"""Validate the dedicated UK private-data Hugging Face credential."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Literal

from huggingface_hub import get_hf_file_metadata, hf_hub_url
from pydantic import BaseModel, ConfigDict, Field

from policyengine_simulation_contract.hf_dataset import parse_hf_dataset_uri
from policyengine_simulation_executor.release_bundle import (
    get_country_release_bundle,
)

HF_RUNTIME_ENV_NAME = "HUGGING_FACE_TOKEN"
HF_MODAL_SECRET_NAME = "pe-uk-private-hf-read-token"
EXPECTED_HF_TOKEN_DISPLAY_NAME = "pe-uk-private-hf-read-token"


class HFAccessTarget(BaseModel):
    """One immutable Hugging Face artifact required by the UK bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_id: str
    repo_type: Literal["model", "dataset"]
    path: str
    revision: str


class HFFineGrainedScope(BaseModel):
    """Permissions reported for one token scope."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    permissions: tuple[str, ...] = ()


class HFFineGrainedPermissions(BaseModel):
    """Fine-grained permissions returned by Hugging Face whoami-v2."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    global_permissions: tuple[str, ...] = Field(default=(), alias="global")
    scoped: tuple[HFFineGrainedScope, ...] = ()


class HFAccessTokenIdentity(BaseModel):
    """Non-secret token metadata returned by Hugging Face."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    display_name: str = Field(alias="displayName")
    role: str
    fine_grained: HFFineGrainedPermissions = Field(alias="fineGrained")


class HFAuthIdentity(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    access_token: HFAccessTokenIdentity = Field(alias="accessToken")


class HFWhoAmIIdentity(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str
    auth: HFAuthIdentity


class HFArtifactAccess(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: HFAccessTarget
    commit_hash: str | None
    etag: str | None
    size: int | None


class HFCredentialAudit(BaseModel):
    """Safe validation output; it never contains the credential value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token_fingerprint: str
    account: str
    token_display_name: str
    artifacts: tuple[HFArtifactAccess, ...]


WhoAmILoader = Callable[[str], Mapping[str, object]]
MetadataLoader = Callable[[HFAccessTarget, str], HFArtifactAccess]


def build_uk_bundle_access_plan() -> tuple[HFAccessTarget, ...]:
    """Derive private Hugging Face artifacts from the installed .py bundle."""

    bundle = get_country_release_bundle("uk")
    targets: dict[tuple[str, str, str, str], HFAccessTarget] = {}
    for dataset_name, dataset_uri in bundle.dataset_uris.items():
        parsed = parse_hf_dataset_uri(dataset_uri)
        if parsed is None:
            continue
        if parsed.revision is None:
            raise RuntimeError(
                f"UK bundle dataset {dataset_name!r} does not pin a revision"
            )
        repo_type = bundle.dataset_repo_types.get(dataset_name)
        if repo_type == "model":
            validated_repo_type: Literal["model", "dataset"] = "model"
        elif repo_type == "dataset":
            validated_repo_type = "dataset"
        else:
            raise RuntimeError(
                f"UK bundle dataset {dataset_name!r} has unsupported Hugging Face "
                f"repository type {repo_type!r}"
            )
        target = HFAccessTarget(
            repo_id=parsed.repo_id,
            repo_type=validated_repo_type,
            path=parsed.path,
            revision=parsed.revision,
        )
        key = (target.repo_id, target.repo_type, target.path, target.revision)
        targets[key] = target
    if not targets:
        raise RuntimeError("The UK .py bundle contains no Hugging Face artifacts")
    return tuple(targets[key] for key in sorted(targets))


def _load_whoami(token: str) -> Mapping[str, object]:
    from huggingface_hub import HfApi

    return HfApi().whoami(token=token)


def _load_metadata(target: HFAccessTarget, token: str) -> HFArtifactAccess:
    metadata = get_hf_file_metadata(
        hf_hub_url(
            repo_id=target.repo_id,
            filename=target.path,
            repo_type=target.repo_type,
            revision=target.revision,
        ),
        token=token,
    )
    return HFArtifactAccess(
        target=target,
        commit_hash=metadata.commit_hash,
        etag=metadata.etag,
        size=metadata.size,
    )


def validate_uk_private_hf_access(
    targets: tuple[HFAccessTarget, ...],
    *,
    environment: Mapping[str, str] = os.environ,
    whoami_loader: WhoAmILoader = _load_whoami,
    metadata_loader: MetadataLoader = _load_metadata,
) -> HFCredentialAudit:
    """Validate identity, read-only permissions, and every planned artifact."""

    token = environment.get(HF_RUNTIME_ENV_NAME)
    if not token:
        raise RuntimeError(f"{HF_RUNTIME_ENV_NAME} is required")
    if not targets:
        raise RuntimeError("At least one Hugging Face artifact must be checked")

    identity = HFWhoAmIIdentity.model_validate(whoami_loader(token))
    token_identity = identity.auth.access_token
    if token_identity.display_name != EXPECTED_HF_TOKEN_DISPLAY_NAME:
        raise RuntimeError(
            f"Unexpected Hugging Face token identity: {token_identity.display_name!r}"
        )
    if token_identity.role != "fineGrained":
        raise RuntimeError("The UK private-data token must be fine-grained")

    permissions = set(token_identity.fine_grained.global_permissions)
    permissions.update(
        permission
        for scope in token_identity.fine_grained.scoped
        for permission in scope.permissions
    )
    if "repo.content.read" not in permissions:
        raise RuntimeError("The UK private-data token has no repository read scope")
    if any("write" in permission for permission in permissions):
        raise RuntimeError("The UK private-data token must not have write permission")

    artifacts = tuple(metadata_loader(target, token) for target in targets)
    return HFCredentialAudit(
        token_fingerprint=sha256(token.encode("utf-8")).hexdigest()[:12],
        account=identity.name,
        token_display_name=token_identity.display_name,
        artifacts=artifacts,
    )

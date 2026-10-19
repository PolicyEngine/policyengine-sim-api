"""Validate the dedicated UK private-data Hugging Face credential."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Literal

from huggingface_hub import get_hf_file_metadata, hf_hub_url
from policyengine_simulation_contract.hf_dataset import parse_hf_dataset_uri
from pydantic import BaseModel, ConfigDict, Field

from policyengine_simulation_executor.release_bundle import (
    get_country_release_bundle,
)

HF_RUNTIME_ENV_NAME = "HUGGING_FACE_TOKEN"
HF_MODAL_SECRET_NAME = "pe-uk-private-hf-read-token"  # noqa: S105  # the Modal secret's name, not its value
EXPECTED_HF_TOKEN_DISPLAY_NAME = "pe-uk-private-hf-read-token"  # noqa: S105  # the token's Hugging Face display name, not its value


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
    """The ``auth`` block of a whoami-v2 response: the token that made the call."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    access_token: HFAccessTokenIdentity = Field(alias="accessToken")


class HFWhoAmIIdentity(BaseModel):
    """The whoami-v2 fields the audit reads: account name and calling token."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str
    auth: HFAuthIdentity


class HFArtifactAccess(BaseModel):
    """Proof that the token can read one target: its resolved file metadata."""

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
            msg = f"UK bundle dataset {dataset_name!r} does not pin a revision"
            raise RuntimeError(msg)
        repo_type = bundle.dataset_repo_types.get(dataset_name)
        if repo_type == "model":
            validated_repo_type: Literal["model", "dataset"] = "model"
        elif repo_type == "dataset":
            validated_repo_type = "dataset"
        else:
            msg = (
                f"UK bundle dataset {dataset_name!r} has unsupported Hugging Face "
                f"repository type {repo_type!r}"
            )
            raise RuntimeError(msg)
        target = HFAccessTarget(
            repo_id=parsed.repo_id,
            repo_type=validated_repo_type,
            path=parsed.path,
            revision=parsed.revision,
        )
        key = (target.repo_id, target.repo_type, target.path, target.revision)
        targets[key] = target
    if not targets:
        msg = "The UK .py bundle contains no Hugging Face artifacts"
        raise RuntimeError(msg)
    return tuple(targets[key] for key in sorted(targets))


def _load_whoami(token: str) -> Mapping[str, object]:
    from huggingface_hub import (  # noqa: PLC0415  # huggingface_hub stays out of this module's import-time imports
        HfApi,
    )

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
        msg = f"{HF_RUNTIME_ENV_NAME} is required"
        raise RuntimeError(msg)
    if not targets:
        msg = "At least one Hugging Face artifact must be checked"
        raise RuntimeError(msg)

    identity = HFWhoAmIIdentity.model_validate(whoami_loader(token))
    token_identity = identity.auth.access_token
    if token_identity.display_name != EXPECTED_HF_TOKEN_DISPLAY_NAME:
        msg = f"Unexpected Hugging Face token identity: {token_identity.display_name!r}"
        raise RuntimeError(msg)
    if token_identity.role != "fineGrained":
        msg = "The UK private-data token must be fine-grained"
        raise RuntimeError(msg)

    permissions = set(token_identity.fine_grained.global_permissions)
    permissions.update(
        permission
        for scope in token_identity.fine_grained.scoped
        for permission in scope.permissions
    )
    if "repo.content.read" not in permissions:
        msg = "The UK private-data token has no repository read scope"
        raise RuntimeError(msg)
    if any("write" in permission for permission in permissions):
        msg = "The UK private-data token must not have write permission"
        raise RuntimeError(msg)

    artifacts = tuple(metadata_loader(target, token) for target in targets)
    return HFCredentialAudit(
        token_fingerprint=sha256(token.encode("utf-8")).hexdigest()[:12],
        account=identity.name,
        token_display_name=token_identity.display_name,
        artifacts=artifacts,
    )


def validate_configured_uk_private_hf_access() -> HFCredentialAudit:
    """Validate the configured token against every artifact in the UK bundle."""
    return validate_uk_private_hf_access(build_uk_bundle_access_plan())

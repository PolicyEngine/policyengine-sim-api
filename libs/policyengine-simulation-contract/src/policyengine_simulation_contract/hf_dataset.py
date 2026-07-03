"""Hugging Face dataset reference helpers.

The gateway image is intentionally small and does not install
``huggingface_hub``. These helpers use the same REST endpoint as
``HfApi.dataset_info(repo_id, revision=...)`` so both gateway and worker
code can validate explicit dataset revisions without adding another runtime
dependency to the gateway.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
HF_REQUEST_TIMEOUT_SECONDS = 30
HF_TOKEN_ENV_VARS = (
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
    "HUGGINGFACE_TOKEN",
)


class HuggingFaceDatasetReferenceError(ValueError):
    """Raised when a Hugging Face dataset reference is invalid."""


@dataclass(frozen=True)
class HFDatasetReference:
    repo_id: str
    path: str
    revision: str | None


def _hf_token() -> str | None:
    for env_name in HF_TOKEN_ENV_VARS:
        value = os.environ.get(env_name)
        if value:
            return value
    return None


def parse_hf_dataset_uri(dataset_uri: str) -> HFDatasetReference | None:
    """Parse an ``hf://`` dataset artifact URI.

    PolicyEngine release manifests use ``hf://org/repo/path@revision``. The
    Hub API needs ``org/repo`` and ``revision`` separately, while path
    validation needs the artifact path within the repo.
    """

    if not dataset_uri.startswith("hf://"):
        return None

    without_scheme = dataset_uri.removeprefix("hf://")
    path_with_repo, revision = (
        without_scheme.rsplit("@", maxsplit=1)
        if "@" in without_scheme
        else (without_scheme, None)
    )
    parts = path_with_repo.split("/", maxsplit=2)
    if len(parts) != 3 or not all(parts):
        raise HuggingFaceDatasetReferenceError(
            f"Invalid Hugging Face dataset URI: {dataset_uri!r}"
        )
    return HFDatasetReference(
        repo_id=f"{parts[0]}/{parts[1]}",
        path=parts[2],
        revision=revision,
    )


@lru_cache
def _fetch_hf_dataset_revision(
    repo_id: str,
    revision: str,
    token: str | None,
) -> dict[str, Any]:
    url = (
        f"{HF_ENDPOINT}/api/datasets/"
        f"{quote(repo_id, safe='/')}/revision/{quote(revision, safe='')}"
    )
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=HF_REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.reason or f"HTTP {exc.code}"
        raise HuggingFaceDatasetReferenceError(
            f"Hugging Face dataset revision {repo_id}@{revision} was not found: "
            f"{detail}"
        ) from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise HuggingFaceDatasetReferenceError(
            f"Unable to validate Hugging Face dataset revision "
            f"{repo_id}@{revision}: {exc}"
        ) from exc


def _siblings_contain_path(payload: dict[str, Any], path: str) -> bool | None:
    siblings = payload.get("siblings")
    if not isinstance(siblings, list):
        return None

    seen_file_listing = False
    for sibling in siblings:
        if not isinstance(sibling, dict):
            continue
        name = sibling.get("rfilename") or sibling.get("path")
        if isinstance(name, str):
            seen_file_listing = True
            if name == path:
                return True
    return False if seen_file_listing else None


def validate_hf_dataset_uri(dataset_uri: str) -> str:
    """Validate an explicit ``hf://`` dataset URI if it pins a revision."""

    parsed = parse_hf_dataset_uri(dataset_uri)
    if parsed is None or parsed.revision is None:
        return dataset_uri

    payload = _fetch_hf_dataset_revision(parsed.repo_id, parsed.revision, _hf_token())
    contains_path = _siblings_contain_path(payload, parsed.path)
    if contains_path is False:
        raise HuggingFaceDatasetReferenceError(
            f"Hugging Face dataset revision {parsed.repo_id}@{parsed.revision} "
            f"does not contain artifact {parsed.path!r}"
        )
    return dataset_uri


def with_hf_revision(dataset_uri: str, revision: str) -> str:
    """Return ``dataset_uri`` pinned to ``revision`` and validate it on the Hub."""

    if not dataset_uri.startswith("hf://"):
        return dataset_uri
    without_revision = dataset_uri.rsplit("@", maxsplit=1)[0]
    return validate_hf_dataset_uri(f"{without_revision}@{revision}")

"""Tests for the self-contained Stage 12 image cache fetch."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.modal.stage12_cache_image import fetch_stage12_cache


class _Blob:
    def __init__(self, payload: bytes):
        self.payload = payload

    def download_to_filename(self, filename: str) -> None:
        Path(filename).write_bytes(self.payload)


class _Client:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads

    def bucket(self, _name: str):
        return SimpleNamespace(blob=lambda path: _Blob(self.payloads[path]))


def _manifest() -> tuple[dict, dict[str, bytes]]:
    artifacts = []
    payloads = {}
    for kind, count in (("dataset", 3), ("baseline", 60)):
        for index in range(count):
            filename = f"{kind}-{index}.h5"
            path = f"{kind}s/us/{index}/{filename}"
            payload = f"{kind}-{index}".encode()
            payloads[path] = payload
            artifacts.append(
                {
                    "type": kind,
                    "path": path,
                    "filename": filename,
                    "year": (2026, 2027, 2025)[index % 3],
                    "identity_digest": f"identity-{kind}-{index}",
                    "content_sha256": sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            )
    return (
        {
            "schema": "stage12-mf1",
            "country": "us",
            "years": [2026, 2027, 2025],
            "partition_sha256": "p" * 64,
            "bundle": {
                "bundle_manifest_sha256": "b" * 64,
                "policyengine_version": "5.2.0",
                "core_package_version": "3.0.0",
                "country_package_version": "1.0.0",
                "data_package_version": "2.0.0",
                "data_artifact_revision": "revision",
                "default_dataset": "populace_us_2024",
            },
            "artifacts": artifacts,
        },
        payloads,
    )


def _digest(manifest: dict) -> str:
    return sha256(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def test_fetches_and_verifies_all_cache_files(monkeypatch, tmp_path) -> None:
    manifest, payloads = _manifest()
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    monkeypatch.setenv("STAGE12_BUNDLE_MANIFEST_SHA256", "b" * 64)
    monkeypatch.setenv("STAGE12_CACHE_MANIFEST_DIGEST", _digest(manifest))

    fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    assert len(list(tmp_path.glob("*.h5"))) == 63
    assert (tmp_path / ".stage12-cache-manifest.json").exists()


def test_rejects_foreign_or_corrupt_cache(monkeypatch, tmp_path) -> None:
    manifest, payloads = _manifest()
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    monkeypatch.setenv("STAGE12_BUNDLE_MANIFEST_SHA256", "different")
    monkeypatch.setenv("STAGE12_CACHE_MANIFEST_DIGEST", _digest(manifest))
    with pytest.raises(RuntimeError, match="another installed bundle"):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    monkeypatch.setenv("STAGE12_BUNDLE_MANIFEST_SHA256", "b" * 64)
    corrupt_path = manifest["artifacts"][0]["path"]
    payloads[corrupt_path] = b"corrupt"
    with pytest.raises(RuntimeError, match=r"size differs|digest differs"):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))


def _use_deployment(monkeypatch, tmp_path, manifest: dict) -> None:
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    monkeypatch.setenv("STAGE12_BUNDLE_MANIFEST_SHA256", "b" * 64)
    monkeypatch.setenv("STAGE12_CACHE_MANIFEST_DIGEST", _digest(manifest))


def _without_one_baseline(manifest: dict) -> None:
    manifest["artifacts"] = manifest["artifacts"][:-1]


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        pytest.param(
            lambda manifest: manifest.update(schema="stage12-mf0"),
            RuntimeError,
            "^Stage 12 cache manifest schema is unsupported$",
            id="schema",
        ),
        pytest.param(
            lambda manifest: manifest.update(bundle=None),
            RuntimeError,
            "^Stage 12 cache manifest names another installed bundle$",
            id="no-bundle",
        ),
        pytest.param(
            lambda manifest: manifest.update(years=[2026, 2027]),
            RuntimeError,
            "^Stage 12 cache manifest has an unexpected year set$",
            id="years",
        ),
        pytest.param(
            lambda manifest: manifest.update(artifacts={}),
            TypeError,
            "^Stage 12 cache manifest has no artifact list$",
            id="no-artifact-list",
        ),
        pytest.param(
            _without_one_baseline,
            RuntimeError,
            "^Stage 12 cache manifest has an unexpected artifact count$",
            id="artifact-count",
        ),
    ],
)
def test_rejects_a_manifest_before_downloading_anything(
    monkeypatch, tmp_path, change, error, message
) -> None:
    manifest, payloads = _manifest()
    change(manifest)
    _use_deployment(monkeypatch, tmp_path, manifest)

    with pytest.raises(error, match=message):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("bucket", "has_manifest"),
    [("", True), ("cache-bucket", False)],
    ids=["no-bucket", "no-manifest"],
)
def test_rejects_a_deployment_without_a_bucket_or_manifest(
    monkeypatch, tmp_path, bucket, *, has_manifest
) -> None:
    manifest, payloads = _manifest()
    _use_deployment(monkeypatch, tmp_path, manifest)

    with pytest.raises(
        RuntimeError, match=r"^Stage 12 cache deployment has no bucket or manifest$"
    ):
        fetch_stage12_cache(
            bucket, manifest if has_manifest else None, client=_Client(payloads)
        )


def test_rejects_a_manifest_whose_digest_differs_from_the_deployment(
    monkeypatch, tmp_path
) -> None:
    manifest, payloads = _manifest()
    _use_deployment(monkeypatch, tmp_path, manifest)
    monkeypatch.setenv("STAGE12_CACHE_MANIFEST_DIGEST", "0" * 64)

    with pytest.raises(
        RuntimeError, match=r"^Stage 12 cache manifest digest differs from deployment$"
    ):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    assert list(tmp_path.iterdir()) == []


def test_rejects_an_artifact_entry_that_is_not_an_object(monkeypatch, tmp_path) -> None:
    manifest, payloads = _manifest()
    manifest["artifacts"].append("not an artifact")
    _use_deployment(monkeypatch, tmp_path, manifest)

    with pytest.raises(TypeError, match=r"^Stage 12 cache artifact entry is invalid$"):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    assert not (tmp_path / ".stage12-cache-manifest.json").exists()


def _with_artifact(manifest: dict, index: int, **changes) -> None:
    manifest["artifacts"][index] = {**manifest["artifacts"][index], **changes}


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            lambda manifest: _with_artifact(manifest, 0, filename="sub/dataset-0.h5"),
            id="filename-with-a-directory",
        ),
        pytest.param(
            lambda manifest: _with_artifact(
                manifest, 1, filename=manifest["artifacts"][0]["filename"]
            ),
            id="duplicate-filename",
        ),
        pytest.param(
            lambda manifest: _with_artifact(manifest, 0, size_bytes="9"),
            id="size-not-an-integer",
        ),
    ],
)
def test_rejects_invalid_artifact_metadata(monkeypatch, tmp_path, change) -> None:
    manifest, payloads = _manifest()
    change(manifest)
    _use_deployment(monkeypatch, tmp_path, manifest)

    with pytest.raises(
        RuntimeError, match=r"^Stage 12 cache artifact metadata is invalid$"
    ):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))


def test_rejects_a_same_size_file_with_another_digest_and_leaves_no_partial(
    monkeypatch, tmp_path
) -> None:
    manifest, payloads = _manifest()
    first_path = manifest["artifacts"][0]["path"]
    payloads[first_path] = bytes(reversed(payloads[first_path]))
    _use_deployment(monkeypatch, tmp_path, manifest)

    with pytest.raises(RuntimeError, match=r"^Stage 12 cache artifact digest differs$"):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "credentials_encoding",
    ["unset", "object", "string-encoded-object"],
)
def test_builds_its_storage_client_from_the_deployment_credentials(
    monkeypatch, tmp_path, credentials_encoding
) -> None:
    from google.cloud import storage
    from google.oauth2 import service_account

    manifest, payloads = _manifest()
    _use_deployment(monkeypatch, tmp_path, manifest)
    info = {"project_id": "cache-project", "type": "service_account"}
    if credentials_encoding == "object":
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", json.dumps(info))
    elif credentials_encoding == "string-encoded-object":
        monkeypatch.setenv(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON", json.dumps(json.dumps(info))
        )
    else:
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    credential_infos = []
    client_arguments = []

    def from_service_account_info(value):
        credential_infos.append(value)
        return "service-account-credentials"

    def build_client(**arguments):
        client_arguments.append(arguments)
        return _Client(payloads)

    monkeypatch.setattr(
        service_account.Credentials,
        "from_service_account_info",
        from_service_account_info,
    )
    monkeypatch.setattr(storage, "Client", build_client)

    fetch_stage12_cache("cache-bucket", manifest)

    if credentials_encoding == "unset":
        assert credential_infos == []
        assert client_arguments == [{}]
    else:
        assert credential_infos == [info]
        assert client_arguments == [
            {"credentials": "service-account-credentials", "project": "cache-project"}
        ]
    assert len(list(tmp_path.glob("*.h5"))) == 63


REGIONAL_DATASET = "populace_us_2024_acs_local"


def _state_artifact(year: int, state_code: str, **changes) -> tuple[dict, bytes]:
    filename = f"{REGIONAL_DATASET}_year_{year}.h5"
    payload = f"state-{year}-{state_code}".encode()
    artifact = {
        "type": "state_dataset",
        "path": f"state-datasets/us/{year}-{state_code}/{filename}",
        "filename": filename,
        "folder": f"state-datasets/{REGIONAL_DATASET}/{state_code}",
        "state_code": state_code,
        "year": year,
        "identity_digest": f"identity-state-{year}-{state_code}",
        "content_sha256": sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        **changes,
    }
    return artifact, payload


def _manifest_with_state_datasets(states=("UT", "WY")) -> tuple[dict, dict]:
    manifest, payloads = _manifest()
    for year in (2026, 2027, 2025):
        for state_code in states:
            artifact, payload = _state_artifact(year, state_code)
            manifest["artifacts"].append(artifact)
            payloads[artifact["path"]] = payload
    return manifest, payloads


def test_places_each_state_file_in_its_state_folder(monkeypatch, tmp_path) -> None:
    manifest, payloads = _manifest_with_state_datasets()
    _use_deployment(monkeypatch, tmp_path, manifest)

    fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    state_folder = tmp_path / "state-datasets" / REGIONAL_DATASET
    for year in (2026, 2027, 2025):
        for state_code in ("UT", "WY"):
            stored = state_folder / state_code / f"{REGIONAL_DATASET}_year_{year}.h5"
            assert stored.read_bytes() == f"state-{year}-{state_code}".encode()
    assert len(list(tmp_path.glob("*.h5"))) == 63


def _drop_one_state_file(manifest: dict) -> None:
    manifest["artifacts"] = [
        item
        for item in manifest["artifacts"]
        if not (item["type"] == "state_dataset" and item["year"] == 2027)
        or item["state_code"] != "WY"
    ]


def _add_unknown_type(manifest: dict) -> None:
    manifest["artifacts"].append({**manifest["artifacts"][0], "type": "mystery"})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            _drop_one_state_file,
            "^Stage 12 cache manifest's state datasets differ between years$",
            id="state-missing-in-one-year",
        ),
        pytest.param(
            _add_unknown_type,
            "^Stage 12 cache manifest has an unexpected artifact type$",
            id="unknown-type",
        ),
    ],
)
def test_rejects_inconsistent_state_datasets_before_downloading(
    monkeypatch, tmp_path, change, message
) -> None:
    manifest, payloads = _manifest_with_state_datasets()
    change(manifest)
    _use_deployment(monkeypatch, tmp_path, manifest)

    with pytest.raises(RuntimeError, match=message):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "folder",
    [
        "/absolute/UT",
        "state-datasets/../../escape",
        "state-datasets/./UT",
        "state-datasets//UT",
        "state-datasets\\UT",
        "",
        None,
    ],
)
def test_rejects_an_unsafe_state_folder_before_downloading(
    monkeypatch, tmp_path, folder
) -> None:
    manifest, payloads = _manifest_with_state_datasets()
    manifest["artifacts"][-1]["folder"] = folder
    _use_deployment(monkeypatch, tmp_path, manifest)

    with pytest.raises(
        RuntimeError, match=r"^Stage 12 cache artifact metadata is invalid$"
    ):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    assert list(tmp_path.iterdir()) == []


def test_rejects_two_entries_for_one_state_file_before_downloading(
    monkeypatch, tmp_path
) -> None:
    manifest, payloads = _manifest_with_state_datasets()
    repeated = dict(manifest["artifacts"][-1])
    manifest["artifacts"].append({**repeated, "path": "state-datasets/us/other/x.h5"})
    payloads["state-datasets/us/other/x.h5"] = payloads[repeated["path"]]
    _use_deployment(monkeypatch, tmp_path, manifest)

    with pytest.raises(
        RuntimeError, match=r"^Stage 12 cache artifact metadata is invalid$"
    ):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    assert list(tmp_path.iterdir()) == []

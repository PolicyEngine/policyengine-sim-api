"""The v2 application's images, and what their build steps read when deploying.

Every v2 image starts from the same packages and the refreshed CA store
(``_v2_package_image``). The worker and coordinator images add the bundle's
certified data for their countries; the US worker image also holds the
published US cache, read from the cache manifest the deploy names
(``_deploy_time_cache_inputs``). The household image holds packages and
source only. ``src/modal/v2_app.py`` attaches the images to its functions and
re-exports every name here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine_simulation_executor.hf_access_validation import (
    HF_MODAL_SECRET_NAME,
    HF_RUNTIME_ENV_NAME,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_cache.models import CacheManifest
from policyengine_simulation_executor.stage12_cache.store import Stage12CacheStore
from policyengine_simulation_observability.observability import (
    modal_image_environment,
)
from src.modal.bundle_data import bundle_data_install_command
from src.modal.ca_certificates import GRPC_CA_BUNDLE_ENV, OPERATING_SYSTEM_CA_PACKAGE
from src.modal.stage12_cache_image import fetch_stage12_cache
from src.modal.static_runtime_files import add_static_runtime_files

import modal

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_bundle import (
        CountryId,
        Stage12CountryBundle,
    )

STAGE12_DATA_DIR = "/opt/policyengine/stage12-data"
_UV_PROJECT_DIR = str(Path(__file__).resolve().parents[2]) if modal.is_local() else "."
RESOLVED_BUNDLE = load_stage12_bundle()


def _deploy_time_cache_inputs() -> tuple[str, dict | None, dict | None]:
    """Resolve the cache manifest, and the contents it must hold, when deploying.

    The contents (years and counts) come from the level handlers' settings,
    without loading the US model; the image download step checks the
    manifest against them.
    """
    if not modal.is_local():
        return "", None, None
    from policyengine_simulation_executor.stage12_cache.precompute_planning import (  # noqa: PLC0415  # needed on the deploying machine only
        expected_cache_contents,
    )

    contents = expected_cache_contents("us")
    bucket = os.environ.get("STAGE12_CACHE_BUCKET", "")
    digest = os.environ.get("STAGE12_CACHE_MANIFEST_DIGEST")
    if not digest:
        return bucket, None, contents
    payload = Stage12CacheStore(bucket).read_manifest(digest)
    if payload is None:
        msg = "Stage 12 cache manifest is unavailable"
        raise RuntimeError(msg)
    manifest = CacheManifest.model_validate(payload)
    if manifest.bundle.bundle_manifest_sha256 != RESOLVED_BUNDLE.bundle_manifest_sha256:
        msg = "Stage 12 cache manifest names another bundle"
        raise RuntimeError(msg)
    return bucket, manifest.canonical_payload(), contents


STAGE12_CACHE_BUCKET, STAGE12_CACHE_MANIFEST, STAGE12_CACHE_CONTENTS = (
    _deploy_time_cache_inputs()
)


gcp_secret = modal.Secret.from_name("stage12-evaluation-gcp-credentials")
hf_secret = modal.Secret.from_name(
    HF_MODAL_SECRET_NAME,
    required_keys=[HF_RUNTIME_ENV_NAME],
)


def _country_bundle(country: CountryId) -> Stage12CountryBundle:
    return next(
        item for item in RESOLVED_BUNDLE.bundle.countries if item.country == country
    )


def _v2_package_image() -> modal.Image:
    """Return the base of every v2 image: the CA store and the locked packages."""
    return (
        modal.Image.debian_slim(python_version="3.13")
        # Upgrade Debian's CA store before anything is downloaded, and make
        # grpcio verify servers against it (src/modal/ca_certificates.py).
        .apt_install(OPERATING_SYSTEM_CA_PACKAGE)
        .env(GRPC_CA_BUNDLE_ENV)
        .uv_sync(
            uv_project_dir=_UV_PROJECT_DIR,
            frozen=True,
            extra_options="--only-group modal-simulation-image",
            # The repository's uv release, from .uv-version.
            uv_version="0.13.6",
        )
    )


def _add_v2_sources(image: modal.Image) -> modal.Image:
    """Copy the source packages and static runtime files every v2 image runs."""
    image = image.add_local_python_source(
        "src.modal",
        "policyengine_simulation_executor",
        "policyengine_simulation_observability",
        "policyengine_simulation_contract",
        "policyengine_simulation_persistence",
        "policyengine_stage12_persistence",
        copy=True,
    )
    return add_static_runtime_files(image, uv_project_dir=_UV_PROJECT_DIR)


def build_v2_image(
    countries: tuple[CountryId, ...], *, include_us_cache: bool = False
) -> modal.Image:
    """Build a Stage 12 image with the bundle's certified data for ``countries``.

    The bundle manifest digest and per-country bundle values are baked into
    the image environment, which worker validation checks at run time. With
    ``include_us_cache``, the image also holds the published US cache.
    """
    country_values = {
        country: _country_bundle(country).model_dump(mode="json")
        for country in countries
    }
    image = (
        _v2_package_image()
        .run_commands(
            bundle_data_install_command(
                RESOLVED_BUNDLE.bundle.policyengine_version,
                countries=countries,
                data_dir=STAGE12_DATA_DIR,
            ),
            secrets=[hf_secret],
        )
        .env(
            {
                **modal_image_environment(),
                "POLICYENGINE_DATA_FOLDER": STAGE12_DATA_DIR,
                "STAGE12_BUNDLE_MANIFEST_SHA256": (
                    RESOLVED_BUNDLE.bundle_manifest_sha256
                ),
                "STAGE12_BUNDLE_VALUES": json.dumps(
                    country_values,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "STAGE12_CACHE_MANIFEST_DIGEST": os.environ.get(
                    "STAGE12_CACHE_MANIFEST_DIGEST", ""
                ),
            }
        )
    )
    if include_us_cache:
        image = image.run_function(
            fetch_stage12_cache,
            args=(STAGE12_CACHE_BUCKET, STAGE12_CACHE_MANIFEST, STAGE12_CACHE_CONTENTS),
            secrets=[gcp_secret],
            cpu=2.0,
            memory=4096,
            # The national files and the state datasets (a file per state for
            # each precomputed year), downloaded and hashed in turn.
            timeout=3600,
        )
    return _add_v2_sources(image)


def build_household_image() -> modal.Image:
    """Build the household functions' image: packages and source only.

    A household calculation reads no dataset, cache or bucket, so the image
    downloads none and its build needs no secret. A function given a
    household job records it in the database at run time, with the secret
    attached to the function. The installed PolicyEngine.py package names the
    bundle the functions calculate with.
    """
    return _add_v2_sources(_v2_package_image().env(modal_image_environment()))


us_worker_image = build_v2_image(("us",), include_us_cache=True)
uk_worker_image = build_v2_image(("uk",))
coordinator_image = build_v2_image(("us", "uk"))
household_image = build_household_image()

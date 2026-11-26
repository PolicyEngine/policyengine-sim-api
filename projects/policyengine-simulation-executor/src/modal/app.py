"""PolicyEngine Simulation versioned Modal app.

This app contains the heavy simulation workload. Its two simulation workers
are Modal classes whose containers restore from memory snapshots with the
country models already loaded (see ``src/modal/country_model_preload.py``).
Each deployment creates a versioned app (e.g., policyengine-simulation-py4-10-0).

The gateway app (policyengine-simulation-gateway) routes requests to these
versioned apps.
"""

import contextlib
import itertools
import os
import random
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.version import InvalidVersion, Version
from policyengine_simulation_executor.hf_access_validation import (
    HF_MODAL_SECRET_NAME,
    HF_RUNTIME_ENV_NAME,
)
from policyengine_simulation_executor.job_progress import (
    NULL_JOB_PROGRESS,
    StoredJobProgressReporter,
)
from policyengine_simulation_executor.release_bundle import (
    get_bundled_country_model_version,
    get_bundled_package_version,
)
from policyengine_simulation_observability.observability import (
    init_process_observability,
    modal_image_environment,
)
from policyengine_simulation_observability.stages import (
    ANNUAL_IMPACT_STAGES,
    BUDGET_WINDOW_STAGES,
    SEGMENTED_NATIONAL_STAGES,
    Stage,
)
from policyengine_simulation_observability.telemetry import (
    normalize_observability_context,
)
from src.modal._image_setup import check_country_models_build, fetch_artifacts
from src.modal.bundle_data import bundle_data_install_command
from src.modal.ca_certificates import GRPC_CA_BUNDLE_ENV, OPERATING_SYSTEM_CA_PACKAGE
from src.modal.country_model_preload import preload_country_models
from src.modal.logging_redaction import redact_params_for_logging
from src.modal.static_runtime_files import add_static_runtime_files

import modal

if TYPE_CHECKING:
    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_executor.job_progress import JobProgressReporter


# Containers restored from a memory snapshot reload Secret values only from
# Modal 1.6.4, and each app's containers run the Modal client that deployed it.
# This project's uv.lock pins such a client; the check refuses a deploy or run
# from an environment with an older one, such as a global Modal install.
FIRST_MODAL_CLIENT_RELOADING_SECRETS_ON_RESTORE = Version("1.6.4")


def _require_secret_reloading_modal_client(client_version: str) -> None:
    """Raise unless ``client_version`` is Modal 1.6.4 or newer.

    Versions compare as Python packaging defines them, so 1.6.4's development
    builds and release candidates count as older than 1.6.4.
    """
    try:
        reloads_secrets = (
            Version(client_version) >= FIRST_MODAL_CLIENT_RELOADING_SECRETS_ON_RESTORE
        )
    except InvalidVersion:
        reloads_secrets = False
    if not reloads_secrets:
        msg = (
            f"Modal client {client_version} is not "
            f"{FIRST_MODAL_CLIENT_RELOADING_SECRETS_ON_RESTORE} or newer: containers "
            "restored from this app's memory snapshots would keep the Secret values "
            "from when each snapshot was taken. Deploy and run from this project's "
            "locked environment, for example "
            "`uv run --frozen modal deploy src/modal/app.py`."
        )
        raise RuntimeError(msg)


if modal.is_local():
    _require_secret_reloading_modal_client(modal.__version__)


def _version_from_env_or_local_bundle_package(env_var: str, package: str) -> str:
    value = os.environ.get(env_var)
    if value:
        return value
    if modal.is_local():
        return get_bundled_package_version(package)
    msg = (
        f"{env_var} must be set in the Modal image environment for remote "
        "simulation workers."
    )
    raise RuntimeError(msg)


def _version_from_env_or_local_bundle(env_var: str, country: str) -> str:
    value = os.environ.get(env_var)
    if value:
        return value
    if modal.is_local():
        return get_bundled_country_model_version(country)
    msg = (
        f"{env_var} must be set in the Modal image environment for remote "
        "simulation workers."
    )
    raise RuntimeError(msg)


POLICYENGINE_VERSION = _version_from_env_or_local_bundle_package(
    "POLICYENGINE_VERSION",
    "policyengine",
)
POLICYENGINE_CORE_VERSION = _version_from_env_or_local_bundle_package(
    "POLICYENGINE_CORE_VERSION",
    "policyengine-core",
)
US_VERSION = _version_from_env_or_local_bundle("POLICYENGINE_US_VERSION", "us")
UK_VERSION = _version_from_env_or_local_bundle("POLICYENGINE_UK_VERSION", "uk")
SIMULATION_BUNDLE_DATA_DIR = os.environ.get(
    "POLICYENGINE_BUNDLE_DATA_DIR",
    "/opt/policyengine/data",
)
SIMULATION_BUNDLE_RECEIPT = (
    f"{SIMULATION_BUNDLE_DATA_DIR}/.policyengine-bundle-receipt.json"
)
VERSION_ENV = {
    "POLICYENGINE_VERSION": POLICYENGINE_VERSION,
    "POLICYENGINE_CORE_VERSION": POLICYENGINE_CORE_VERSION,
    "POLICYENGINE_US_VERSION": US_VERSION,
    "POLICYENGINE_UK_VERSION": UK_VERSION,
    "POLICYENGINE_DATA_FOLDER": SIMULATION_BUNDLE_DATA_DIR,
    "POLICYENGINE_BUNDLE_RECEIPT": SIMULATION_BUNDLE_RECEIPT,
}


def get_app_name(policyengine_version: str) -> str:
    """Generate versioned app name from the policyengine.py package version.

    Replaces dots with dashes for URL safety.
    Example: 4.10.0 -> policyengine-simulation-py4-10-0
    """
    policyengine_safe = policyengine_version.replace(".", "-")
    return f"policyengine-simulation-py{policyengine_safe}"


# App name can be overridden via environment variable, otherwise generated from versions
APP_NAME = os.environ.get("MODAL_APP_NAME", get_app_name(POLICYENGINE_VERSION))

# App definition with versioned name
app = modal.App(APP_NAME)

# Secrets
# GCP credentials are shared across environments (always from main)
gcp_secret = modal.Secret.from_name("gcp-credentials", environment_name="main")
hf_secret = modal.Secret.from_name(
    HF_MODAL_SECRET_NAME,
    required_keys=[HF_RUNTIME_ENV_NAME],
)
OBSERVABILITY_ENV = modal_image_environment()


# Only meaningful locally: image definitions are built on the deploying
# machine. Inside containers this module loads as the entrypoint at
# /root/app.py, where parents[2] does not exist (and the project dir is
# never read container-side).
_UV_PROJECT_DIR = str(Path(__file__).resolve().parents[2]) if modal.is_local() else "."


# The deploy-time variable naming each country's precomputed manifest. Only
# the US has prebuilt files today; a country is added here when its
# precompute publishes a manifest.
MANIFEST_DIGEST_VARIABLES = {"us": "POLICYENGINE_MANIFEST_DIGEST"}


def _deploy_time_artifact_inputs(country: str = "us") -> tuple[str, dict | None]:
    """Resolve the bucket and a country's deploy manifest on the deploying machine.

    The manifest content rides into the fetch layer's args, so Modal's
    layer cache busts exactly when the deployed artifact set changes.
    Resolution must never hard-fail at import: this module is imported
    where no digest can exist (inside containers, by the precompute app
    that produces the digest, by the PR image smoke), so a missing digest
    yields a None manifest and fetch_artifacts fails at build time
    instead — a digest-less deploy cannot silently ship an artifact-less
    image.

    A resolved manifest is validated as ArtifactManifest and crosses into
    the layer as its canonical payload: fetch_artifacts cannot import the
    model by design (the layer is import-restricted), so the dict exists
    only at that serialization boundary.
    """
    if not modal.is_local():
        return "", None
    digest = os.environ.get(MANIFEST_DIGEST_VARIABLES[country])
    bucket = os.environ.get("POLICYENGINE_ARTIFACT_BUCKET", "")
    if not digest:
        return bucket, None
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # deploy-time manifest check only; the app import stays light for the precompute and smoke apps
        ArtifactStore,
    )
    from policyengine_simulation_executor.precompute_models import (  # noqa: PLC0415  # deploy-time manifest check only; the app import stays light for the precompute and smoke apps
        ArtifactManifest,
    )

    store = ArtifactStore(bucket or None)
    payload = store.read_manifest(digest)
    if payload is None:
        msg = (
            f"Artifact manifest {digest} is not in the store: the deploy "
            "must consume a digest published by the precompute run."
        )
        raise RuntimeError(msg)
    # Validate on the runner so a corrupt or foreign manifest fails the
    # deploy here, not mid-image-build.
    manifest = ArtifactManifest.model_validate(payload)
    if manifest.country != country:
        msg = (
            f"{MANIFEST_DIGEST_VARIABLES[country]} names a {manifest.country} "
            f"manifest, not a {country} one."
        )
        raise RuntimeError(msg)
    return store.bucket_name, manifest.canonical_payload()


_ARTIFACT_INPUTS = {
    country: _deploy_time_artifact_inputs(country)
    for country in MANIFEST_DIGEST_VARIABLES
}


def build_runtime_simulation_image() -> modal.Image:
    """Build the image layers up to the version env (no artifact fetch or snapshot).

    Shared by the deployed app and the image smoke app
    (src/modal/smoke_app.py): all must construct these layers through
    this one code path so their definitions — and therefore Modal's
    content-addressed layer cache keys — are identical.
    """
    return (
        modal.Image.debian_slim(python_version="3.13")
        # Upgrade Debian's CA store before anything is downloaded, and make
        # grpcio verify servers against it (src/modal/ca_certificates.py).
        .apt_install(OPERATING_SYSTEM_CA_PACKAGE)
        .env(GRPC_CA_BUNDLE_ENV)
        # The modal-simulation-image dependency group, installed straight
        # from this project's uv.lock (frozen): image packages match the
        # tested environment and can only change through a relock.
        # --only-group keeps the project's local packages out. The
        # policyengine[models] requirement declares the wrapper's model
        # packages, and uv.lock supplies their exact resolved versions.
        .uv_sync(
            uv_project_dir=_UV_PROJECT_DIR,
            frozen=True,
            extra_options="--only-group modal-simulation-image",
            # The repository's uv release, from .uv-version.
            uv_version="0.13.6",
        )
        # Packages are already installed from the frozen lock. The wrapper CLI
        # downloads only the certified datasets and writes their receipt.
        .run_commands(
            bundle_data_install_command(
                POLICYENGINE_VERSION,
                countries=("us", "uk"),
                data_dir=SIMULATION_BUNDLE_DATA_DIR,
            ),
            secrets=[hf_secret],
        )
        .env(VERSION_ENV)
        .env(OBSERVABILITY_ENV)
    )


def build_base_simulation_image() -> modal.Image:
    """Runtime image layers plus one artifact download step per country, US first."""
    image = build_runtime_simulation_image()
    # Bake each country's precomputed files (year files and current-law
    # baselines) into the image's prebuilt folders. These layers MUST stay
    # before add_local_python_source — that layer is keyed on source file
    # hashes, so anything after it rebuilds on every code change. Each
    # layer's args carry its country's content-addressed manifest, so its
    # cache busts exactly when that country's file set changes, and a later
    # country's change rebuilds only its own step and the ones after it. UK
    # requests still build datasets at request time.
    for country in MANIFEST_DIGEST_VARIABLES:
        bucket, manifest = _ARTIFACT_INPUTS[country]
        image = image.run_function(
            fetch_artifacts,
            args=(bucket, manifest),
            secrets=[gcp_secret],
            cpu=2.0,
            memory=4096,
            # The US national files and baselines are about 430 MB; the
            # per-state year files (51 states for each year) add several GB.
            timeout=3600,
        )
    return image


# Heavy image for the simulation workers. Its last layer checks that both
# countries' models build; the workers load them in their snapshot hook.
simulation_image = add_static_runtime_files(
    build_base_simulation_image().add_local_python_source(
        "src.modal",
        "policyengine_simulation_executor",
        "policyengine_simulation_observability",
        "policyengine_simulation_contract",
        copy=True,
    ),
    uv_project_dir=_UV_PROJECT_DIR,
).run_function(check_country_models_build)


def _configure_modal_observability(
    *,
    service_role: str,
) -> "ObservabilityRuntime":
    return init_process_observability(
        service_name=APP_NAME,
        platform="modal",
        service_role=service_role,
        environment=os.getenv("MODAL_ENVIRONMENT", "local"),
        service_version=POLICYENGINE_VERSION,
    )


class ContainerStart(StrEnum):
    """How the container serving a worker call started: its ``container_start``."""

    RESTORED_FROM_SNAPSHOT = "restored_from_snapshot"
    TOOK_SNAPSHOT = "took_snapshot"
    NO_SNAPSHOT = "no_snapshot"


class _ContainerCalls:
    """Says how this container started, and numbers the worker calls it serves.

    A Modal container serves one worker class or function, so this module's
    state is the container's. The workers' snapshot hook records whether
    Modal snapshots the container it runs in, and that container's
    ``MODAL_TASK_ID``, before Modal takes the snapshot. A container restored
    from the snapshot starts with that record in memory, but Modal 1.6.4 sets
    its ``MODAL_TASK_ID`` to its own task ID, so the two IDs differ. The
    recorded ID is only compared, never reported: it names the snapshotting
    container, not the one serving the call.

    The call counter is in the snapshot too, but no call runs before Modal
    takes it, so every container's first call is number 1.
    """

    def __init__(self) -> None:
        self._snapshot_taken_here = False
        self._snapshot_task_id: str | None = None
        self._call_numbers = itertools.count(1)

    def record_snapshot_hook(self) -> None:
        """Record, from the snapshot hook, whether Modal snapshots this container.

        Modal 1.6.4 runs the hook in every container that isn't restored, and
        takes a snapshot only when the Modal process that starts the container
        has set ``MODAL_ENABLE_SNAP_RESTORE`` to ``1`` (``maybe_snapshot`` in
        ``modal/_runtime/user_code_imports.py``). The flag is set last, so a
        failure partway leaves the container reporting ``no_snapshot``.
        """
        self._snapshot_task_id = os.environ.get("MODAL_TASK_ID")
        self._snapshot_taken_here = os.environ.get("MODAL_ENABLE_SNAP_RESTORE") == "1"

    def container_start(self) -> ContainerStart:
        """Compare this container's task ID with the snapshotting container's."""
        if not self._snapshot_taken_here:
            return ContainerStart.NO_SNAPSHOT
        if os.environ.get("MODAL_TASK_ID") == self._snapshot_task_id:
            return ContainerStart.TOOK_SNAPSHOT
        return ContainerStart.RESTORED_FROM_SNAPSHOT

    def next_call_attributes(self) -> dict[str, str | int]:
        """Return the next call's two container attributes, numbering the call.

        The call takes its number first, so it counts even if reading the
        container start fails.
        """
        call_number = next(self._call_numbers)
        return {
            "container_start": self.container_start().value,
            "container_call_number": call_number,
        }


_CONTAINER_CALLS = _ContainerCalls()


def _set_modal_call_attributes(runtime: "ObservabilityRuntime") -> None:
    # Telemetry is non-fatal: a failed attribute must never fail the Modal call.
    # Each attribute set is attempted on its own, so one failure keeps the other.
    with contextlib.suppress(Exception):
        runtime.set_context(function_call_id=modal.current_function_call_id())
    with contextlib.suppress(Exception):
        runtime.set_context(**_CONTAINER_CALLS.next_call_attributes())


def _job_progress_reporter() -> "JobProgressReporter":
    """Return a stage reporter for this call, keyed by its public ``job_id``.

    The gateway's ``job_id`` is the spawned call's ``object_id``, which is
    this call's ``current_function_call_id()``. Without one there is nothing
    to key the record by, so nothing is reported.

    Budget-window children are ordinary ``run_simulation`` calls, so each
    writes a record under its own call id. ``/budget-window-jobs`` does not
    read those records yet; the writes are a few small Dict puts per year.
    """
    from policyengine_simulation_contract.job_progress_state import (  # noqa: PLC0415  # resolved per call so tests can patch job_progress_state
        get_job_progress,
        put_job_progress,
    )

    try:
        job_id = modal.current_function_call_id()
    except Exception:  # noqa: BLE001  # stage reporting is non-fatal
        job_id = None
    if not job_id:
        return NULL_JOB_PROGRESS
    return StoredJobProgressReporter(
        job_id, write=put_job_progress, read=get_job_progress
    )


def _record_stage_durations(
    runtime: "ObservabilityRuntime", progress: "JobProgressReporter"
) -> None:
    """Attach the job's stage durations to its operation telemetry.

    Main-path traces cannot split the baseline from the reform calculation:
    both run inside ``economic_impact_analysis``. These attributes can. The
    durations add up over attempts when Modal runs the call again.
    """
    durations = progress.stage_durations()
    if not durations:
        return
    # Telemetry is non-fatal.
    with contextlib.suppress(Exception):
        runtime.set_context(
            job_attempts=progress.attempts(),
            **{
                f"job_stage_seconds_{stage.value}": seconds
                for stage, seconds in durations.items()
            },
        )


def _run_simulation(
    params: dict,
    *,
    observability_context: dict | None,
) -> dict:
    """Execute one annual economic simulation.

    The job behind ``SimulationWorker.run_simulation``. Imports the
    implementation in the call; in a restored container the snapshot hook has
    already loaded the country models it uses. Emits redacted operation data
    through the shared observability runtime. Records the job's stage for
    status polls (see ``job_progress``).
    """
    progress = _job_progress_reporter()
    progress.start()
    runtime = _configure_modal_observability(service_role="simulation_worker")

    # We deliberately avoid sending full ``params`` or ``result`` blobs to
    # observability: they can embed signed URLs, reform
    # parameter trees with sensitive policy details, or result payloads
    # large enough to blow attribute budgets. Correlation fields come from
    # the validated remote context while the heavy payload stays in memory.
    redacted_params = {
        **redact_params_for_logging(params),
        "modal_app_name": APP_NAME,
        "modal_environment": os.getenv("MODAL_ENVIRONMENT", "local"),
        "modal_function_name": "run_simulation",
    }
    try:
        propagated = normalize_observability_context(observability_context)
        with runtime.operation(
            ANNUAL_IMPACT_STAGES.name(Stage.ANNUAL_EXECUTION),
            attributes=redacted_params,
            remote_context=propagated,
        ):
            _set_modal_call_attributes(runtime)
            from src.modal.segmented_national import (  # noqa: PLC0415  # Modal function body: loaded in the worker container, not at deploy time
                dispatch_run_simulation,
            )

            try:
                # Plain national macro requests fan out across region groups
                # by default (segmented: false opts out). APP_NAME is this
                # deployed app; children spawn into its dedicated
                # SimulationSegmentWorker pool.
                return dispatch_run_simulation(
                    params,
                    app_name=APP_NAME,
                    runtime=runtime,
                    progress=progress,
                )
            except KeyboardInterrupt:
                # Modal preempts with SIGINT and runs the call again; the
                # record stays running for that attempt.
                progress.interrupt()
                raise
            finally:
                # Close the last stage inside the operation, so its
                # completion record carries every stage duration.
                progress.finish()
                _record_stage_durations(runtime, progress)
    except KeyboardInterrupt:
        # Preempted before the calculation started.
        progress.interrupt()
        raise
    finally:
        # A no-op unless the operation never started.
        progress.finish()
        runtime.shutdown()


def _run_simulation_segment(
    params: dict,
    *,
    observability_context: dict | None,
) -> dict:
    """Run one region-group child of a segmented national run.

    The job behind ``SimulationSegmentWorker.run_simulation_segment``. Calls
    the monolithic impl directly — children are ``region_group`` requests and
    never re-segment.
    """
    runtime = _configure_modal_observability(service_role="simulation_worker")
    redacted_params = {
        **redact_params_for_logging(params),
        "modal_app_name": APP_NAME,
        "modal_environment": os.getenv("MODAL_ENVIRONMENT", "local"),
        "modal_function_name": "run_simulation_segment",
    }
    try:
        propagated = normalize_observability_context(observability_context)
        with runtime.operation(
            SEGMENTED_NATIONAL_STAGES.name(Stage.SEGMENTED_NATIONAL_EXECUTION),
            attributes=redacted_params,
            remote_context=propagated,
        ):
            _set_modal_call_attributes(runtime)
            from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # Modal function body: loaded in the worker container, not at deploy time
                run_simulation_impl,
            )

            return run_simulation_impl(params, runtime=runtime)
    finally:
        runtime.shutdown()


class _CountryModelsLoadedBeforeSnapshot:
    """Base of the simulation workers: loads the models before the snapshot.

    Modal runs ``snap=True`` hooks in every container that isn't restored, and
    takes the memory snapshot from such a container when it starts it with
    snapshots enabled; every container restored from the snapshot starts with
    the loaded models in memory. ``snap=False`` hooks run after each restore.
    Observability is configured per call instead, after the restore, so each
    restored container reports its own process identity.
    """

    @modal.enter(snap=True)
    def load_country_models(self) -> None:
        """Load both countries' models and the SPM capability into the snapshot.

        First records whether Modal snapshots this container, and its task ID,
        so each call can report how its container started (``_ContainerCalls``).
        Telemetry is non-fatal: a failed record must never stop the preload.
        """
        with contextlib.suppress(Exception):
            _CONTAINER_CALLS.record_snapshot_hook()
        preload_country_models()

    @modal.enter(snap=False)
    def reseed_random_after_restore(self) -> None:
        """Give each restored container its own random sequence.

        The snapshot holds the state of Python's ``random`` module, and Python
        reseeds it only after a fork, so every restored container would draw
        the same numbers: OpenTelemetry's span IDs, for one, which would then
        repeat across the segment children of one trace.
        """
        random.seed()


@app.cls(
    image=simulation_image,
    cpu=8.0,
    memory=32768,
    timeout=3600,
    retries=0,
    max_containers=100,
    secrets=[gcp_secret, hf_secret],
    enable_memory_snapshot=True,
)
class SimulationWorker(_CountryModelsLoadedBeforeSnapshot):
    """Runs annual simulations for the gateway and budget-window batches."""

    @modal.method()
    def run_simulation(
        self,
        params: dict,
        *,
        observability_context: dict | None = None,
    ) -> dict:
        """Execute one annual economic simulation (see ``_run_simulation``)."""
        return _run_simulation(params, observability_context=observability_context)


@app.cls(
    image=simulation_image,
    # A segment child calculates one region group in one process, so it
    # keeps one CPU busy. Measured on Modal with both countries' models
    # loaded, it peaks at about 3.4 GiB for a year whose dataset and
    # baseline are baked into the image, and at about 6.9 GiB for a year
    # whose dataset and baseline it builds itself. The memory is a request,
    # not a limit.
    cpu=1.0,
    memory=10240,
    timeout=3600,
    retries=0,
    max_containers=300,
    secrets=[gcp_secret, hf_secret],
    enable_memory_snapshot=True,
)
class SimulationSegmentWorker(_CountryModelsLoadedBeforeSnapshot):
    """Runs the region-group children of segmented national runs.

    A separate class from ``SimulationWorker`` so segment children draw from
    their own container pool: a blocking segmented parent must never share a
    bounded pool with the children it waits on.
    """

    @modal.method()
    def run_simulation_segment(
        self,
        params: dict,
        *,
        observability_context: dict | None = None,
    ) -> dict:
        """Run one region-group child (see ``_run_simulation_segment``)."""
        return _run_simulation_segment(
            params, observability_context=observability_context
        )


@app.function(
    image=simulation_image,
    cpu=1.0,
    memory=4096,
    timeout=3600,
    retries=0,
    max_containers=100,
    secrets=[gcp_secret, hf_secret],
)
def run_budget_window_batch(
    params: dict,
    *,
    observability_context: dict | None = None,
) -> dict:
    """Execute a multi-year budget-window batch orchestration."""
    runtime = _configure_modal_observability(service_role="budget_window_worker")

    redacted_params = {
        **redact_params_for_logging(params),
        "modal_app_name": APP_NAME,
        "modal_environment": os.getenv("MODAL_ENVIRONMENT", "local"),
        "modal_function_name": "run_budget_window_batch",
    }
    try:
        propagated = normalize_observability_context(observability_context)
        with runtime.operation(
            BUDGET_WINDOW_STAGES.name(Stage.BUDGET_WINDOW_EXECUTION),
            attributes=redacted_params,
            remote_context=propagated,
        ):
            _set_modal_call_attributes(runtime)
            from src.modal.budget_window_batch import (  # noqa: PLC0415  # Modal function body: loaded in the worker container, not at deploy time
                run_budget_window_batch_impl,
            )

            return run_budget_window_batch_impl(params, runtime=runtime)
    finally:
        runtime.shutdown()

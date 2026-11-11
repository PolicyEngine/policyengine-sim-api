"""Checks of bounded Stage 12 deployment validation and its Modal secret sync."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".github/scripts/stage12-validate-infrastructure.sh"
SYNC_SCRIPT = REPO_ROOT / ".github/scripts/stage12-sync-modal-secrets.sh"
DEPLOY_WORKFLOW = REPO_ROOT / ".github/workflows/simulation-deploy.reusable.yml"


def _run_bash(
    *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run bash on a repository script, capturing text output; a failure is returned."""
    return subprocess.run(
        ["bash", *arguments],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_infrastructure_validation_is_bounded_and_valid_shell() -> None:
    result = _run_bash("-n", str(SCRIPT))
    assert result.returncode == 0, result.stderr
    sync_result = _run_bash("-n", str(SYNC_SCRIPT))
    assert sync_result.returncode == 0, sync_result.stderr
    source = SCRIPT.read_text(encoding="utf-8")
    assert "STAGE12_ENVIRONMENT" in source
    assert "STAGE12_ARTIFACT_BUCKET" in source
    assert "STAGE12_CACHE_BUCKET" in source
    assert "V2_SIMULATION_OUTPUT_BUCKET" in source
    assert (
        "Pre-provisioned Stage 12 database, secret, report storage, simulation "
        "output storage, cache storage, and UK geography lookup access" in source
    )
    assert "STAGE12_DATABASE_URL_SECRET_NAME" in source
    assert "policyengine_v2_runtime" in source
    assert r"${expected_role}\.[a-z0-9]{20}" in source
    assert "stage12_infrastructure" in source
    assert "gcloud secrets versions list" in source
    assert "gcloud secrets versions access" in source
    assert "gcloud secrets create" not in source
    assert "gcloud secrets versions add" not in source
    assert "gcloud iam service-accounts create" not in source
    assert "gcloud iam service-accounts keys create" not in source
    assert "gcloud storage buckets create" not in source
    assert "gcloud storage buckets update" not in source
    assert "add-iam-policy-binding" not in source
    assert "storage cp" in source
    assert "storage rm" in source
    assert "stage-12-runs/_deployment-validation" in source
    assert "get-iam-policy" in source
    assert "STAGE12_DATABASE_ADMIN_URL" not in source
    assert "psql" not in source
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source

    modal_sync = SYNC_SCRIPT.read_text(encoding="utf-8")
    assert "--from-json" in modal_sync
    assert "stage12-evaluation-runtime" in modal_sync
    assert "stage12-evaluation-gcp-credentials" in modal_sync
    assert "STAGE12_DATABASE_URL:" in modal_sync
    assert "STAGE12_CACHE_BUCKET:" in modal_sync
    assert "V2_SIMULATION_OUTPUT_BUCKET:" in modal_sync
    assert "GOOGLE_APPLICATION_CREDENTIALS_JSON:" in modal_sync


def test_infrastructure_validation_rejects_missing_configuration() -> None:
    result = _run_bash(str(SCRIPT), env={})
    assert result.returncode != 0
    assert "STAGE12_ENVIRONMENT is required" in result.stderr


def test_storage_validation_checks_runtime_and_deployment_credentials() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    helper_start = source.index("runtime_gcloud()")
    helper_end = source.index("\n}\n", helper_start)
    helper = source[helper_start:helper_end]

    for inherited_variable in (
        "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_GHA_CREDS_PATH",
    ):
        assert f"-u {inherited_variable}" in helper

    assert "runtime_gcloud auth activate-service-account" in source
    modal_account = '--account="${STAGE12_MODAL_SERVICE_ACCOUNT}"'
    assert source.count(f"runtime_gcloud {modal_account} storage cp") == 7
    assert source.count(f"runtime_gcloud {modal_account} storage rm") == 6
    assert (
        'gcloud storage cp \\\n  "${cache_canary_object}" '
        '"${canary_download_file}" --quiet'
    ) in source
    assert "gs://${STAGE12_CACHE_BUCKET}/_deployment-validation" in source
    assert (
        "gs://${V2_SIMULATION_OUTPUT_BUCKET}/simulation-runs/_deployment-validation"
        in source
    )
    assert "gs://policyengine-uk-data-private/constituencies_2024.csv" in source
    assert "gs://policyengine-uk-data-private/local_authorities_2021.csv" not in source
    assert "STAGE12_UK_GEOGRAPHY_BUCKET" not in source


def test_modal_secret_sync_rejects_missing_configuration() -> None:
    result = _run_bash(str(SYNC_SCRIPT), "staging", env={})
    assert result.returncode != 0
    assert "STAGE12_GCP_PROJECT_ID is required" in result.stderr


def test_infrastructure_validation_installs_its_locked_dependencies() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("  configure_stage12_v2:")
    section = workflow[start : workflow.index("\n  deploy_stage12_v2:", start)]

    assert "actions/setup-python@v6" in section
    assert "astral-sh/setup-uv@v8.1.0" in section
    assert "uv sync --frozen" in section
    assert section.index("uv sync --frozen") < section.index(
        "stage12-validate-infrastructure.sh"
    )


PROJECT_ID = "policyengine-simulation-entry"
MODAL_SERVICE_ACCOUNT = f"stage12-modal-staging@{PROJECT_ID}.iam.gserviceaccount.com"
ENTRYPOINT_SERVICE_ACCOUNT = (
    f"simulation-entry-staging@{PROJECT_ID}.iam.gserviceaccount.com"
)
DEPLOY_SERVICE_ACCOUNT = f"simulation-entry-deploy@{PROJECT_ID}.iam.gserviceaccount.com"
ARTIFACT_BUCKET = "policyengine-stage12-staging-artifacts"
CACHE_BUCKET = "policyengine-stage12-staging-cache"
OUTPUT_BUCKET = "policyengine-v2-staging-simulation-outputs"
DATABASE_URL = (
    "postgresql://policyengine_v2_runtime.abcdefghijklmnopqrst:password"
    "@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require"
)
CREDENTIALS = json.dumps(
    {"type": "service_account", "client_email": MODAL_SERVICE_ACCOUNT}
)
SECRET_NAMES = {
    "STAGE12_MODAL_TOKEN_ID_SECRET_NAME": "modal-token-id",
    "STAGE12_MODAL_TOKEN_SECRET_SECRET_NAME": "modal-token-secret",
    "STAGE12_DATABASE_URL_SECRET_NAME": "stage12-staging-database-url",
    "STAGE12_GCP_CREDENTIALS_SECRET_NAME": "stage12-staging-modal-gcp-credentials",
}

# Stands in for gcloud: Secret Manager values come from a JSON file, and
# Cloud Storage objects are files under FAKE_GCS_OBJECTS/<bucket>/<path>.
FAKE_GCLOUD = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json
    import os
    import shutil
    import sys
    from pathlib import Path

    with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as log:
        log.write(" ".join(sys.argv[1:]) + "\\n")
    arguments = [value for value in sys.argv[1:] if not value.startswith("--account=")]
    objects = Path(os.environ["FAKE_GCS_OBJECTS"])
    secrets = json.loads(Path(os.environ["FAKE_SECRETS"]).read_text(encoding="utf-8"))


    def object_file(uri):
        return objects / uri.removeprefix("gs://")


    if arguments[:3] == ["secrets", "versions", "list"]:
        print("1")
    elif arguments[:3] == ["secrets", "versions", "access"]:
        sys.stdout.write(secrets[arguments[arguments.index("--secret") + 1]])
    elif arguments[:2] == ["secrets", "get-iam-policy"]:
        members = [
            f"serviceAccount:{account}"
            for account in os.environ["FAKE_SECRET_ACCESSORS"].split()
        ]
        binding = {"role": "roles/secretmanager.secretAccessor", "members": members}
        print(json.dumps({"bindings": [binding]}))
    elif arguments[:2] == ["auth", "activate-service-account"]:
        pass
    elif arguments[:2] == ["storage", "cp"]:
        source, destination = arguments[2], arguments[3]
        if source.startswith("gs://"):
            shutil.copyfile(object_file(source), destination)
        else:
            object_file(destination).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, object_file(destination))
    elif arguments[:2] == ["storage", "rm"]:
        object_file(arguments[2]).unlink()
    else:
        sys.exit(f"unexpected gcloud call: {arguments}")
""")

# Stands in for uv: a Modal secret is kept as FAKE_MODAL_SECRETS/<name>.json.
FAKE_UV = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os
    import shutil
    import sys
    from pathlib import Path

    arguments = sys.argv[1:]
    with open(os.environ["FAKE_UV_LOG"], "a", encoding="utf-8") as log:
        log.write(" ".join(arguments) + "\\n")
    if arguments[2:5] == ["modal", "secret", "create"]:
        secret_file = Path(os.environ["FAKE_MODAL_SECRETS"]) / f"{arguments[5]}.json"
        shutil.copyfile(arguments[arguments.index("--from-json") + 1], secret_file)
""")


def _fake_command_environment(tmp_path: Path) -> dict[str, str]:
    """Return an environment whose gcloud and uv are the fakes above."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, source in (("gcloud", FAKE_GCLOUD), ("uv", FAKE_UV)):
        command = fake_bin / name
        command.write_text(source, encoding="utf-8")
        command.chmod(0o755)
    for directory in ("objects", "modal-secrets", "tmp"):
        (tmp_path / directory).mkdir()
    lookup = tmp_path / "objects/policyengine-uk-data-private/constituencies_2024.csv"
    lookup.parent.mkdir(parents=True)
    lookup.write_text("code,name\n", encoding="utf-8")
    secrets = tmp_path / "secrets.json"
    secrets.write_text(
        json.dumps(
            {
                SECRET_NAMES["STAGE12_DATABASE_URL_SECRET_NAME"]: f"{DATABASE_URL}\n",
                SECRET_NAMES["STAGE12_GCP_CREDENTIALS_SECRET_NAME"]: CREDENTIALS,
            }
        ),
        encoding="utf-8",
    )
    return {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path / "tmp"),
        "FAKE_GCLOUD_LOG": str(tmp_path / "gcloud-calls.log"),
        "FAKE_UV_LOG": str(tmp_path / "uv-calls.log"),
        "FAKE_GCS_OBJECTS": str(tmp_path / "objects"),
        "FAKE_MODAL_SECRETS": str(tmp_path / "modal-secrets"),
        "FAKE_SECRETS": str(secrets),
        "FAKE_SECRET_ACCESSORS": (
            f"{ENTRYPOINT_SERVICE_ACCOUNT} {DEPLOY_SERVICE_ACCOUNT}"
        ),
        "STAGE12_GCP_PROJECT_ID": PROJECT_ID,
        "STAGE12_ARTIFACT_BUCKET": ARTIFACT_BUCKET,
        "STAGE12_CACHE_BUCKET": CACHE_BUCKET,
        "V2_SIMULATION_OUTPUT_BUCKET": OUTPUT_BUCKET,
        **SECRET_NAMES,
    }


def _validation_environment(tmp_path: Path) -> dict[str, str]:
    return {
        **_fake_command_environment(tmp_path),
        "STAGE12_ENVIRONMENT": "staging",
        "STAGE12_ENTRYPOINT_SERVICE_ACCOUNT": ENTRYPOINT_SERVICE_ACCOUNT,
        "STAGE12_DEPLOY_SERVICE_ACCOUNT": DEPLOY_SERVICE_ACCOUNT,
        "STAGE12_MODAL_SERVICE_ACCOUNT": MODAL_SERVICE_ACCOUNT,
    }


def _sync_environment(tmp_path: Path) -> dict[str, str]:
    return {
        **_fake_command_environment(tmp_path),
        "MODAL_TOKEN_ID": "token-id",
        "MODAL_TOKEN_SECRET": "token-secret",
    }


def _calls(log_path: str) -> list[str]:
    path = Path(log_path)
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_infrastructure_validation_writes_a_canary_to_the_output_bucket(
    tmp_path: Path,
) -> None:
    environment = _validation_environment(tmp_path)

    result = _run_bash(str(SCRIPT), env=environment)

    assert result.returncode == 0, result.stderr
    assert "simulation output storage" in result.stdout
    canary_prefix = f"gs://{OUTPUT_BUCKET}/simulation-runs/_deployment-validation/"
    canary_calls = [
        call for call in _calls(environment["FAKE_GCLOUD_LOG"]) if canary_prefix in call
    ]
    (canary,) = {
        word for call in canary_calls for word in call.split() if canary_prefix in word
    }
    assert canary.startswith(f"{canary_prefix}staging/")
    # The worker's credential writes, reads back and removes the canary.
    account = f"--account={MODAL_SERVICE_ACCOUNT}"
    assert [call.split()[:3] for call in canary_calls] == [
        [account, "storage", "cp"],
        [account, "storage", "cp"],
        [account, "storage", "rm"],
    ]
    assert canary_calls[0].split()[4] == canary
    assert canary_calls[1].split()[3] == canary
    assert not any((tmp_path / "objects" / OUTPUT_BUCKET).rglob("*.txt"))


@pytest.mark.parametrize(
    ("output_bucket", "message"),
    [
        (
            "policyengine-v2-production-simulation-outputs",
            "V2_SIMULATION_OUTPUT_BUCKET must be valid and identify the environment",
        ),
        (
            ARTIFACT_BUCKET,
            "V2_SIMULATION_OUTPUT_BUCKET must differ from STAGE12_ARTIFACT_BUCKET",
        ),
    ],
)
def test_infrastructure_validation_rejects_a_wrong_output_bucket(
    tmp_path: Path, output_bucket: str, message: str
) -> None:
    environment = {
        **_validation_environment(tmp_path),
        "V2_SIMULATION_OUTPUT_BUCKET": output_bucket,
    }

    result = _run_bash(str(SCRIPT), env=environment)

    assert result.returncode == 1
    assert message in result.stderr
    assert _calls(environment["FAKE_GCLOUD_LOG"]) == []


def test_infrastructure_validation_requires_the_output_bucket(tmp_path: Path) -> None:
    environment = _validation_environment(tmp_path)
    del environment["V2_SIMULATION_OUTPUT_BUCKET"]

    result = _run_bash(str(SCRIPT), env=environment)

    assert result.returncode != 0
    assert "V2_SIMULATION_OUTPUT_BUCKET is required" in result.stderr
    assert _calls(environment["FAKE_GCLOUD_LOG"]) == []


def test_modal_secret_sync_gives_workers_the_simulation_output_bucket(
    tmp_path: Path,
) -> None:
    environment = _sync_environment(tmp_path)

    result = _run_bash(str(SYNC_SCRIPT), "staging", env=environment)

    assert result.returncode == 0, result.stderr
    secrets = tmp_path / "modal-secrets"
    runtime = json.loads(
        (secrets / "stage12-evaluation-runtime.json").read_text(encoding="utf-8")
    )
    assert runtime == {
        "STAGE12_DATABASE_URL": DATABASE_URL,
        "STAGE12_ARTIFACT_BUCKET": ARTIFACT_BUCKET,
        "STAGE12_CACHE_BUCKET": CACHE_BUCKET,
        "V2_SIMULATION_OUTPUT_BUCKET": OUTPUT_BUCKET,
    }
    credentials = json.loads(
        (secrets / "stage12-evaluation-gcp-credentials.json").read_text(
            encoding="utf-8"
        )
    )
    assert credentials == {"GOOGLE_APPLICATION_CREDENTIALS_JSON": CREDENTIALS}
    assert all(
        call.endswith("--env staging --force")
        for call in _calls(environment["FAKE_UV_LOG"])
    )


def test_modal_secret_sync_requires_the_simulation_output_bucket(
    tmp_path: Path,
) -> None:
    environment = _sync_environment(tmp_path)
    del environment["V2_SIMULATION_OUTPUT_BUCKET"]

    result = _run_bash(str(SYNC_SCRIPT), "staging", env=environment)

    assert result.returncode != 0
    assert "V2_SIMULATION_OUTPUT_BUCKET is required" in result.stderr
    assert _calls(environment["FAKE_UV_LOG"]) == []
    assert _calls(environment["FAKE_GCLOUD_LOG"]) == []


def test_both_stage12_deploy_steps_receive_the_simulation_output_bucket() -> None:
    jobs = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))["jobs"]

    for job_name, script_name in (
        ("configure_stage12_v2", "stage12-validate-infrastructure.sh"),
        ("deploy_stage12_v2", "stage12-sync-modal-secrets.sh"),
    ):
        job = jobs[job_name]
        # The job's environment supplies the GitHub environment's variables.
        assert job["environment"] == "${{ inputs.release_environment }}"
        (step,) = [step for step in job["steps"] if script_name in step.get("run", "")]
        assert step["env"]["V2_SIMULATION_OUTPUT_BUCKET"] == (
            "${{ vars.V2_SIMULATION_OUTPUT_BUCKET }}"
        )

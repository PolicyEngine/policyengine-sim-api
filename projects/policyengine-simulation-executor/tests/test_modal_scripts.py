"""Unit tests for Modal deployment bash scripts.

These tests verify the bash scripts in .github/scripts/ work correctly.
Tests use subprocess to invoke the scripts and verify their behavior.
"""

import os
import subprocess

import pytest

from fixtures.test_modal_scripts import REPO_ROOT, SCRIPTS_DIR

pytest_plugins = ("fixtures.test_modal_scripts",)


class TestModalExtractVersions:
    """Tests for modal-extract-versions.sh"""

    script = SCRIPTS_DIR / "modal-extract-versions.sh"

    def test_script_exists(self):
        """Script file should exist."""
        assert self.script.exists(), f"Script not found at {self.script}"

    def test_script_is_executable_or_can_be_run_with_bash(self):
        """Script should be runnable with bash."""
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax error in script: {result.stderr}"

    def test_extracts_versions_from_policyengine_bundle(self, temp_github_output):
        """Should extract model and data versions from policyengine.py's bundle."""
        project_dir = REPO_ROOT / "projects" / "policyengine-simulation-executor"

        if not (project_dir / "uv.lock").exists():
            pytest.skip("uv.lock not found in project directory")

        env = os.environ.copy()
        env["GITHUB_OUTPUT"] = temp_github_output

        result = subprocess.run(
            ["bash", str(self.script), str(project_dir)],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0, f"Script failed: {result.stderr}"

        with open(temp_github_output) as f:
            output = f.read()

        assert "policyengine_version=" in output
        assert "policyengine_core_version=" in output
        assert "us_version=" in output
        assert "us_data_version=" in output
        assert "uk_version=" in output
        assert "uk_data_version=" in output

    def test_deploy_workflow_passes_core_version_to_modal(self):
        """Deploy workflow should pass core version into the Modal app build."""
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
        ).read_text(encoding="utf-8")

        assert "policyengine_core_version" in workflow
        assert (
            "POLICYENGINE_CORE_VERSION: ${{ needs.prepare.outputs.policyengine_core_version }}"
            in workflow
        )

    def test_deploy_workflow_threads_force_latest_to_routing_publisher(self):
        """Manual rollback flag should reach the post-deployment routing job."""
        deploy_workflow = (
            REPO_ROOT / ".github" / "workflows" / "simulation-deploy.yml"
        ).read_text(encoding="utf-8")
        reusable_workflow = (
            REPO_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
        ).read_text(encoding="utf-8")

        assert "force_latest:" in deploy_workflow
        assert "force_latest: ${{ inputs.force_latest || false }}" in deploy_workflow
        assert "force_latest:" in reusable_workflow
        assert "args+=(--force-latest)" in reusable_workflow
        assert reusable_workflow.index("deploy_executor:") < reusable_workflow.index(
            "update_routing:"
        )


class TestModalHealthCheck:
    """Tests for modal-health-check.sh"""

    script = SCRIPTS_DIR / "modal-health-check.sh"

    def test_script_exists(self):
        """Script file should exist."""
        assert self.script.exists(), f"Script not found at {self.script}"

    def test_script_syntax(self):
        """Script should have valid bash syntax."""
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_requires_base_url_argument(self):
        """Should fail when no URL is provided."""
        result = subprocess.run(
            ["bash", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Should fail without URL argument"

    def test_fails_on_unreachable_url(self):
        """Should fail when URL is unreachable."""
        result = subprocess.run(
            ["bash", str(self.script), "http://localhost:99999/nonexistent", "1", "1"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Should fail on unreachable URL"


class TestSimulationDeploymentSummary:
    """Tests for simulation-deployment-summary.sh"""

    script = SCRIPTS_DIR / "simulation-deployment-summary.sh"

    def test_script_exists(self):
        """Script file should exist."""
        assert self.script.exists(), f"Script not found at {self.script}"

    def test_script_syntax(self):
        """Script should have valid bash syntax."""
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_generates_success_summary(self, temp_github_step_summary):
        """Should generate markdown summary for successful deployments."""
        env = os.environ.copy()
        env["GITHUB_STEP_SUMMARY"] = temp_github_step_summary

        result = subprocess.run(
            [
                "bash",
                str(self.script),
                "success",
                "https://beta.example.com",
                "success",
                "https://prod.example.com",
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0, f"Script failed: {result.stderr}"

        with open(temp_github_step_summary) as f:
            summary = f.read()

        assert "Simulation API Deployment Summary" in summary
        assert "Beta deployment" in summary
        assert "Prod deployment" in summary
        assert "Candidate URL: https://beta.example.com" in summary
        assert "Stable URL: https://prod.example.com" in summary
        assert "https://beta.example.com" in summary
        assert "https://prod.example.com" in summary

    def test_generates_skipped_summary(self, temp_github_step_summary):
        """Should handle skipped deployments."""
        env = os.environ.copy()
        env["GITHUB_STEP_SUMMARY"] = temp_github_step_summary

        result = subprocess.run(
            [
                "bash",
                str(self.script),
                "skipped",
                "",
                "success",
                "https://prod.example.com",
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0

        with open(temp_github_step_summary) as f:
            summary = f.read()

        assert "Beta deployment" in summary


class TestModalSyncSecrets:
    """Tests for modal-sync-secrets.sh"""

    script = SCRIPTS_DIR / "modal-sync-secrets.sh"

    def test_script_exists(self):
        """Script file should exist."""
        assert self.script.exists(), f"Script not found at {self.script}"

    def test_script_syntax(self):
        """Script should have valid bash syntax."""
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_requires_modal_environment_argument(self):
        """Should fail when no modal environment is provided."""
        result = subprocess.run(
            ["bash", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Should fail without modal environment"

    def test_requires_gh_environment_argument(self):
        """Should fail when no GH environment is provided."""
        result = subprocess.run(
            ["bash", str(self.script), "staging"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Should fail without GH environment"

    def test_fails_when_gateway_auth_config_is_partial(self):
        """Should fail before touching Modal when auth config is partial."""
        env = os.environ.copy()
        env["HF_TOKEN"] = "hf_test"
        env["GATEWAY_AUTH_ISSUER"] = "https://tenant.auth0.com"
        env.pop("GATEWAY_AUTH_AUDIENCE", None)
        env.pop("GATEWAY_AUTH_CLIENT_ID", None)
        env.pop("GATEWAY_AUTH_CLIENT_SECRET", None)

        result = subprocess.run(
            ["bash", str(self.script), "staging", "beta"],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode != 0
        assert "Gateway auth config is partial." in result.stderr

    def test_fails_when_auth_required_but_gateway_auth_vars_missing(self):
        """Required auth must refuse deploy when the GitHub secrets are absent."""
        env = os.environ.copy()
        env["HF_TOKEN"] = "hf_test"
        env["GATEWAY_AUTH_REQUIRED"] = "1"
        for key in (
            "GATEWAY_AUTH_ISSUER",
            "GATEWAY_AUTH_AUDIENCE",
            "GATEWAY_AUTH_CLIENT_ID",
            "GATEWAY_AUTH_CLIENT_SECRET",
        ):
            env.pop(key, None)

        result = subprocess.run(
            ["bash", str(self.script), "staging", "beta"],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode != 0
        assert "GATEWAY_AUTH_REQUIRED is enabled" in result.stderr

    def test_requires_hf_token(self):
        """Should fail before touching Modal when HF_TOKEN is absent."""
        env = os.environ.copy()
        env.pop("HF_TOKEN", None)

        result = subprocess.run(
            ["bash", str(self.script), "staging", "beta"],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode != 0
        assert "HF_TOKEN is required" in result.stderr

    def test_creates_gateway_secret_with_normalized_issuer(self, tmp_path):
        """Should sync HF and runtime gateway values and normalize issuer."""
        uv_calls_log = tmp_path / "uv_calls.log"
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_uv = fake_bin / "uv"
        fake_uv.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$UV_CALLS_LOG"\n')
        fake_uv.chmod(0o755)

        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}:{env['PATH']}",
                "UV_CALLS_LOG": str(uv_calls_log),
                "HF_TOKEN": "hf_test",
                "GATEWAY_AUTH_ISSUER": "https://tenant.auth0.com",
                "GATEWAY_AUTH_AUDIENCE": "https://simulation-api-beta.policyengine.org",
                "GATEWAY_AUTH_CLIENT_ID": "client-id",
                "GATEWAY_AUTH_CLIENT_SECRET": "client-secret",
                "GATEWAY_AUTH_REQUIRED": "1",
            }
        )

        result = subprocess.run(
            ["bash", str(self.script), "staging", "beta"],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        calls = uv_calls_log.read_text()
        assert "run modal secret create huggingface-token" in calls
        assert "HF_TOKEN=hf_test" in calls
        assert "run modal secret create policyengine-gateway-auth" in calls
        assert "GATEWAY_AUTH_ISSUER=https://tenant.auth0.com/" in calls
        assert (
            "GATEWAY_AUTH_AUDIENCE=https://simulation-api-beta.policyengine.org"
            in calls
        )
        assert "GATEWAY_AUTH_REQUIRED=1" in calls
        assert "GATEWAY_AUTH_CLIENT_ID" not in calls
        assert "GATEWAY_AUTH_CLIENT_SECRET" not in calls


class TestModalPrecompute:
    """Tests for modal-precompute.sh"""

    script = SCRIPTS_DIR / "modal-precompute.sh"

    def _run_with_fake_uv(
        self,
        tmp_path,
        *args,
        fake_output="MANIFEST_DIGEST=abc123",
        bucket="policyengine-sim-artifacts",
        with_github_output=True,
    ):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_path = tmp_path / "uv-calls.log"
        github_output = tmp_path / "github-output.txt"
        uv_path = bin_dir / "uv"
        uv_path.write_text(
            "#!/bin/bash\n"
            'printf \'%s\\n\' "$*" >> "$UV_FAKE_LOG"\n'
            "printf '%s\\n' \"$UV_FAKE_OUTPUT\"\n",
            encoding="utf-8",
        )
        uv_path.chmod(0o755)

        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
                "UV_FAKE_LOG": str(log_path),
                "UV_FAKE_OUTPUT": fake_output,
            }
        )
        if with_github_output:
            env["GITHUB_OUTPUT"] = str(github_output)
        else:
            env.pop("GITHUB_OUTPUT", None)
        if bucket is None:
            env.pop("POLICYENGINE_ARTIFACT_BUCKET", None)
        else:
            env["POLICYENGINE_ARTIFACT_BUCKET"] = bucket

        result = subprocess.run(
            ["bash", str(self.script), *args],
            capture_output=True,
            text=True,
            env=env,
        )
        calls = (
            log_path.read_text(encoding="utf-8").splitlines()
            if log_path.exists()
            else []
        )
        github_out = (
            github_output.read_text(encoding="utf-8") if github_output.exists() else ""
        )
        return result, calls, github_out

    def test_script_exists(self):
        """Script file should exist."""
        assert self.script.exists(), f"Script not found at {self.script}"

    def test_script_syntax(self):
        """Script should have valid bash syntax."""
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_requires_modal_environment_argument(self):
        """Should fail when no modal environment is provided."""
        result = subprocess.run(
            ["bash", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Should fail without modal environment"

    def test_requires_artifact_bucket(self, tmp_path):
        """Should fail before invoking modal when the bucket env var is unset."""
        result, calls, _ = self._run_with_fake_uv(tmp_path, "staging", bucket=None)

        assert result.returncode != 0
        assert "POLICYENGINE_ARTIFACT_BUCKET is required" in result.stderr
        assert calls == [], "Should not invoke modal without a bucket"

    def test_requires_github_output_before_running(self, tmp_path):
        """A missing GITHUB_OUTPUT must fail fast, not after the compute."""
        result, calls, _ = self._run_with_fake_uv(
            tmp_path, "staging", with_github_output=False
        )

        assert result.returncode != 0
        assert "GITHUB_OUTPUT is required" in result.stderr
        assert calls == [], "Should not invoke modal without GITHUB_OUTPUT"

    def test_runs_precompute_app_and_exports_digest(self, tmp_path):
        """Default run has no --force and the digest reaches GITHUB_OUTPUT."""
        result, calls, github_out = self._run_with_fake_uv(
            tmp_path,
            "staging",
            fake_output=(
                "Planning against gs://policyengine-sim-artifacts (force=False)\n"
                "MANIFEST_DIGEST=abc123\n"
                "Stopping app - local entrypoint completed."
            ),
        )

        assert result.returncode == 0, result.stderr
        run_call = next(call for call in calls if "modal run" in call)
        assert "run modal run --env=staging src/modal/precompute_app.py" in run_call
        assert "--force" not in run_call
        assert "manifest_digest=abc123" in github_out

    def test_takes_the_last_digest_line(self, tmp_path):
        """The MANIFEST_DIGEST= contract is last-line-wins."""
        result, _, github_out = self._run_with_fake_uv(
            tmp_path,
            "staging",
            fake_output="MANIFEST_DIGEST=old\nMANIFEST_DIGEST=new",
        )

        assert result.returncode == 0, result.stderr
        assert "manifest_digest=new" in github_out
        assert "manifest_digest=old" not in github_out

    def test_passes_force_when_requested(self, tmp_path):
        """A truthy force argument appends --force to the modal run."""
        result, calls, _ = self._run_with_fake_uv(tmp_path, "staging", "true")

        assert result.returncode == 0, result.stderr
        run_call = next(call for call in calls if "modal run" in call)
        assert run_call.endswith("--force")

    def test_fails_when_no_digest_line_is_emitted(self, tmp_path):
        """A run that never prints the digest contract must fail the job."""
        result, _, github_out = self._run_with_fake_uv(
            tmp_path,
            "staging",
            fake_output="Planning against gs://bucket (force=False)",
        )

        assert result.returncode != 0
        assert "no MANIFEST_DIGEST= line" in result.stderr
        assert "manifest_digest=" not in github_out

    def test_deploy_workflow_runs_precompute_before_deploy(self):
        """The executor job must precompute before deploying its Modal app."""
        reusable_workflow = (
            REPO_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
        ).read_text(encoding="utf-8")

        executor_job = reusable_workflow[
            reusable_workflow.index("deploy_executor:") : reusable_workflow.index(
                "update_routing:"
            )
        ]
        assert executor_job.index("Run artifact precompute") < executor_job.index(
            "Deploy versioned Modal executor"
        )
        assert 'modal-precompute.sh "${{ inputs.modal_environment }}"' in executor_job
        assert (
            "POLICYENGINE_ARTIFACT_BUCKET: ${{ vars.POLICYENGINE_ARTIFACT_BUCKET }}"
            in executor_job
        )
        assert (
            "manifest_digest: ${{ steps.precompute.outputs.manifest_digest }}"
            in executor_job
        )

    def test_deploy_workflow_threads_force_recompute_to_script(self):
        """The manual recompute flag should reach the precompute script."""
        deploy_workflow = (
            REPO_ROOT / ".github" / "workflows" / "simulation-deploy.yml"
        ).read_text(encoding="utf-8")
        reusable_workflow = (
            REPO_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
        ).read_text(encoding="utf-8")

        assert "force_recompute:" in deploy_workflow
        assert (
            "force_recompute: ${{ inputs.force_recompute || false }}" in deploy_workflow
        )
        assert "force_recompute:" in reusable_workflow
        assert (
            'modal-precompute.sh "${{ inputs.modal_environment }}" '
            '"${{ inputs.force_recompute }}"' in reusable_workflow
        )

    def test_deploy_workflow_threads_manifest_and_store_credentials_to_deploy(self):
        """The deploy step resolves the manifest from GCS on the runner,
        so it needs the digest, the store bucket, and GCP credentials."""
        reusable_workflow = (
            REPO_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
        ).read_text(encoding="utf-8")

        deploy_step = reusable_workflow[
            reusable_workflow.index(
                "Deploy versioned Modal executor"
            ) : reusable_workflow.index("Run executor unit tests")
        ]
        assert (
            "POLICYENGINE_MANIFEST_DIGEST: "
            "${{ steps.precompute.outputs.manifest_digest }}" in deploy_step
        )
        assert (
            "POLICYENGINE_ARTIFACT_BUCKET: ${{ vars.POLICYENGINE_ARTIFACT_BUCKET }}"
            in deploy_step
        )
        assert "GCP_CREDENTIALS_JSON: ${{ secrets.GCP_CREDENTIALS_JSON }}" in (
            deploy_step
        )
        # The precompute, deploy, and record-marker steps each carry the
        # bucket var.
        assert (
            reusable_workflow.count(
                "POLICYENGINE_ARTIFACT_BUCKET: ${{ vars.POLICYENGINE_ARTIFACT_BUCKET }}"
            )
            == 3
        )

    def test_precompute_is_gated_by_the_shared_secret_sync(self):
        """All three deploy jobs wait for the shared preparation job."""
        reusable_workflow = (
            REPO_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
        ).read_text(encoding="utf-8")

        sync_invocation = (
            'modal-sync-secrets.sh "${{ inputs.modal_environment }}" '
            '"${{ inputs.release_environment }}"'
        )
        assert reusable_workflow.count(sync_invocation) == 1
        executor_job = reusable_workflow[
            reusable_workflow.index("deploy_executor:") : reusable_workflow.index(
                "update_routing:"
            )
        ]
        assert "needs: prepare" in executor_job


class TestModalRecordDeployment:
    """Tests for modal-record-deployment.sh"""

    script = SCRIPTS_DIR / "modal-record-deployment.sh"

    def _run_with_fake_uv(self, tmp_path, *args, bucket="policyengine-sim-artifacts"):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_path = tmp_path / "uv-calls.log"
        uv_path = bin_dir / "uv"
        uv_path.write_text(
            '#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$UV_FAKE_LOG"\n',
            encoding="utf-8",
        )
        uv_path.chmod(0o755)

        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
                "UV_FAKE_LOG": str(log_path),
            }
        )
        if bucket is None:
            env.pop("POLICYENGINE_ARTIFACT_BUCKET", None)
        else:
            env["POLICYENGINE_ARTIFACT_BUCKET"] = bucket

        result = subprocess.run(
            ["bash", str(self.script), *args],
            capture_output=True,
            text=True,
            env=env,
        )
        calls = (
            log_path.read_text(encoding="utf-8").splitlines()
            if log_path.exists()
            else []
        )
        return result, calls

    def test_script_exists(self):
        """Script file should exist."""
        assert self.script.exists(), f"Script not found at {self.script}"

    def test_requires_environment_argument(self):
        """Should fail with no arguments."""
        result = subprocess.run(
            ["bash", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0

    def test_requires_manifest_digest_argument(self, tmp_path):
        """Should fail before invoking anything without a digest."""
        result, calls = self._run_with_fake_uv(tmp_path, "beta")

        assert result.returncode != 0
        assert calls == []

    def test_requires_artifact_bucket(self, tmp_path):
        """Should fail before invoking anything without the bucket env."""
        result, calls = self._run_with_fake_uv(
            tmp_path, "beta", "digest-abc", bucket=None
        )

        assert result.returncode != 0
        assert "POLICYENGINE_ARTIFACT_BUCKET is required" in result.stderr
        assert calls == []

    def test_invokes_the_recorder_module(self, tmp_path):
        """The exact python -m invocation, environment and digest threaded."""
        result, calls = self._run_with_fake_uv(tmp_path, "beta", "digest-abc")

        assert result.returncode == 0, result.stderr
        assert calls == [
            "run python -m src.modal.utils.record_deployment "
            "--environment beta --manifest-digest digest-abc"
        ]

    def test_deploy_workflow_records_marker(self):
        """Both deployment legs write the artifact-GC liveness marker."""
        reusable_workflow = (
            REPO_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
        ).read_text(encoding="utf-8")

        invocation = (
            'modal-record-deployment.sh "${{ inputs.release_environment }}" '
            '"${{ needs.deploy_executor.outputs.manifest_digest }}"'
        )
        assert invocation in reusable_workflow

        # The payload builder maps missing env vars to empty strings, so a
        # silent rename here would empty the marker's fields — assert every
        # env line the step feeds it.
        marker_step = reusable_workflow[
            reusable_workflow.index("Record deployment marker") :
        ]
        marker_step = marker_step[: marker_step.index("authenticated_test:")]
        for env_line in (
            "POLICYENGINE_ARTIFACT_BUCKET: ${{ vars.POLICYENGINE_ARTIFACT_BUCKET }}",
            "GCP_CREDENTIALS_JSON: ${{ secrets.GCP_CREDENTIALS_JSON }}",
            "POLICYENGINE_VERSION: ${{ needs.prepare.outputs.policyengine_version }}",
            "POLICYENGINE_US_VERSION: ${{ needs.prepare.outputs.us_version }}",
            "POLICYENGINE_UK_VERSION: ${{ needs.prepare.outputs.uk_version }}",
            "US_DATA_VERSION: ${{ needs.prepare.outputs.us_data_version }}",
            "UK_DATA_VERSION: ${{ needs.prepare.outputs.uk_data_version }}",
        ):
            assert env_line in marker_step, f"marker step is missing: {env_line}"


class TestModalGetUrl:
    """Tests for modal-get-url.sh"""

    script = SCRIPTS_DIR / "modal-get-url.sh"

    def test_script_exists(self):
        """Script file should exist."""
        assert self.script.exists(), f"Script not found at {self.script}"

    def test_script_syntax(self):
        """Script should have valid bash syntax."""
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_requires_modal_environment_argument(self):
        """Should fail when no modal environment is provided."""
        result = subprocess.run(
            ["bash", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Should fail without modal environment"


class TestModalSetupEnvironments:
    """Tests for modal-setup-environments.sh"""

    script = SCRIPTS_DIR / "modal-setup-environments.sh"

    def test_script_exists(self):
        """Script file should exist."""
        assert self.script.exists(), f"Script not found at {self.script}"

    def test_script_syntax(self):
        """Script should have valid bash syntax."""
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"


class TestSimulationRunIntegTests:
    """Tests for simulation-run-integ-tests.sh"""

    script = SCRIPTS_DIR / "simulation-run-integ-tests.sh"

    def test_script_exists(self):
        """Script file should exist."""
        assert self.script.exists(), f"Script not found at {self.script}"

    def test_script_syntax(self):
        """Script should have valid bash syntax."""
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_requires_environment_argument(self):
        """Should fail when no environment is provided."""
        result = subprocess.run(
            ["bash", str(self.script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Should fail without environment"

    def test_requires_base_url_argument(self):
        """Should fail when no base URL is provided."""
        result = subprocess.run(
            ["bash", str(self.script), "beta"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Should fail without base URL"

    def test_fails_when_auth_config_is_partial(self):
        """Should fail before running tests if token-mint config is partial."""
        env = os.environ.copy()
        env["SIMULATION_TEST_AUTH_ISSUER"] = "https://tenant.auth0.com/"
        env.pop("SIMULATION_TEST_AUTH_AUDIENCE", None)
        env.pop("SIMULATION_TEST_AUTH_CLIENT_ID", None)
        env.pop("SIMULATION_TEST_AUTH_CLIENT_SECRET", None)

        result = subprocess.run(
            ["bash", str(self.script), "beta", "https://example.com"],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode != 0
        assert "Simulation integration-test auth config is partial." in result.stderr

    def test_fails_when_auth_required_but_auth_vars_missing(self):
        """Required auth must not run tests unauthenticated."""
        env = os.environ.copy()
        env["SIMULATION_TEST_AUTH_REQUIRED"] = "1"
        for key in (
            "SIMULATION_TEST_AUTH_ISSUER",
            "SIMULATION_TEST_AUTH_AUDIENCE",
            "SIMULATION_TEST_AUTH_CLIENT_ID",
            "SIMULATION_TEST_AUTH_CLIENT_SECRET",
        ):
            env.pop(key, None)

        result = subprocess.run(
            ["bash", str(self.script), "beta", "https://example.com"],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode != 0
        assert "SIMULATION_TEST_AUTH_REQUIRED is enabled" in result.stderr

    def test_exports_us_and_uk_model_versions_to_integration_tests(self, tmp_path):
        """Deploy-extracted model versions should reach the pytest settings."""
        uv_calls_log = tmp_path / "uv_calls.log"
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_uv = fake_bin / "uv"
        fake_uv.write_text(
            "#!/bin/bash\n"
            'printf "%s|base=%s|us=%s|uk=%s\\n" "$*" '
            '"${simulation_integ_test_base_url:-}" '
            '"${simulation_integ_test_us_model_version:-}" '
            '"${simulation_integ_test_uk_model_version:-}" >> "$UV_CALLS_LOG"\n'
        )
        fake_uv.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["UV_CALLS_LOG"] = str(uv_calls_log)
        for key in (
            "SIMULATION_TEST_AUTH_REQUIRED",
            "SIMULATION_TEST_AUTH_ISSUER",
            "SIMULATION_TEST_AUTH_AUDIENCE",
            "SIMULATION_TEST_AUTH_CLIENT_ID",
            "SIMULATION_TEST_AUTH_CLIENT_SECRET",
        ):
            env.pop(key, None)

        result = subprocess.run(
            [
                "bash",
                str(self.script),
                "prod",
                "https://example.com",
                "1.690.7",
                "2.88.20",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
        )

        assert result.returncode == 0, f"Script failed: {result.stderr}"
        log = uv_calls_log.read_text()
        assert "run pytest tests/simulation/ -v -m not beta_only" in log
        assert "base=https://example.com" in log
        assert "us=1.690.7" in log
        assert "uk=2.88.20" in log


class TestAllScriptsHaveShebang:
    """Verify all scripts have proper shebang and error handling."""

    def test_all_scripts_have_shebang(self, all_modal_scripts):
        """All scripts should start with #!/bin/bash."""
        for script in all_modal_scripts:
            with open(script) as f:
                first_line = f.readline().strip()
            assert first_line == "#!/bin/bash", f"{script.name} missing shebang"

    def test_all_scripts_have_strict_mode(self, all_modal_scripts):
        """All scripts should use set -euo pipefail for safety."""
        for script in all_modal_scripts:
            content = script.read_text()
            assert "set -euo pipefail" in content, f"{script.name} missing strict mode"

    def test_all_scripts_have_valid_syntax(self, all_modal_scripts):
        """All scripts should pass bash syntax check."""
        for script in all_modal_scripts:
            result = subprocess.run(
                ["bash", "-n", str(script)],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, (
                f"{script.name} has syntax errors: {result.stderr}"
            )

"""Deployment-marker recording: pure payload assembly plus the store write.

The deployed/<env>.json marker is the artifact GC's liveness signal, so
main() writes through the real store client seam (faked here) and any
write failure propagates — a deploy that cannot record itself goes red.
"""

from datetime import datetime

import pytest

from src.modal.utils import record_deployment


def _github_env():
    return {
        "POLICYENGINE_VERSION": "4.22.0",
        "POLICYENGINE_US_VERSION": "1.3.0",
        "POLICYENGINE_UK_VERSION": "2.9.0",
        "US_DATA_VERSION": "1.2.3",
        "UK_DATA_VERSION": "3.4.5",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "PolicyEngine/policyengine-sim-api",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_SHA": "abc123",
    }


class TestBuildMarkerPayload:
    def test_records_the_deploy_identity(self):
        payload = record_deployment.build_marker_payload(
            "beta", "digest-1", _github_env()
        )

        assert payload["environment"] == "beta"
        assert payload["manifest_digest"] == "digest-1"
        assert payload["policyengine_version"] == "4.22.0"
        assert payload["us_version"] == "1.3.0"
        assert payload["uk_version"] == "2.9.0"
        assert payload["us_data_version"] == "1.2.3"
        assert payload["uk_data_version"] == "3.4.5"
        assert payload["github_run_id"] == "12345"
        assert payload["github_run_url"] == (
            "https://github.com/PolicyEngine/policyengine-sim-api/actions/runs/12345"
        )
        assert payload["github_sha"] == "abc123"

    def test_timestamp_is_utc_iso8601(self):
        payload = record_deployment.build_marker_payload("beta", "d", {})
        parsed = datetime.fromisoformat(payload["deployed_at"])
        assert parsed.utcoffset() is not None
        assert parsed.utcoffset().total_seconds() == 0

    def test_missing_env_yields_empty_fields_not_errors(self):
        payload = record_deployment.build_marker_payload("prod", "d", {})
        assert payload["github_run_url"] == ""
        assert payload["policyengine_version"] == ""


class TestMain:
    def test_writes_the_marker_through_the_store(self, monkeypatch):
        writes = []

        class FakeStore:
            def __init__(self, bucket_name=None, **kwargs):
                pass

            def write_deployed_marker(self, environment, payload):
                writes.append((environment, payload))

        monkeypatch.setattr(record_deployment, "ArtifactStore", FakeStore)
        monkeypatch.setattr(
            "sys.argv",
            [
                "record_deployment",
                "--environment",
                "beta",
                "--manifest-digest",
                "digest-9",
            ],
        )

        record_deployment.main()

        assert len(writes) == 1
        environment, payload = writes[0]
        assert environment == "beta"
        assert payload["manifest_digest"] == "digest-9"

    def test_requires_both_arguments(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["record_deployment", "--environment", "beta"])
        with pytest.raises(SystemExit):
            record_deployment.main()

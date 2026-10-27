"""Tests for the command line's publication journal modes."""

from __future__ import annotations

import json

import pytest
from registry_test_support import (
    install_routing_store,
    publication_plan,
    run_registry_main,
)
from src.modal.utils import update_version_registry as registry


def test_cli_records_and_completes_release_publications(
    monkeypatch, patched_modal, tmp_path, capsys
):
    plan = publication_plan(monkeypatch, None, ("6.1.0", "1.1.0", "2.2.0"))
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    run_registry_main(monkeypatch, "--record-plan", str(path))
    store = patched_modal["staging/simulation-api-routing-state"]
    assert store.get("active") is None
    assert store.get("publication") == {"status": "pending", "plan": plan}
    run_registry_main(monkeypatch, "--apply-plan", str(path))
    run_registry_main(monkeypatch, "--complete-plan", str(path))
    assert store.get("active") == plan["after"]
    assert store.get("publication") == {"status": "completed", "plan": plan}
    assert ": pending" in capsys.readouterr().out


@pytest.mark.parametrize(
    "finalization_case",
    [
        ("74401", "true", "completed"),
        ("74401", "false", "restored"),
        ("74400", "false", "foreign-publication"),
        (None, "false", "restored"),
    ],
    ids=lambda finalization_case: "-".join(str(part) for part in finalization_case),
)
def test_cli_finalizes_the_printed_journal_and_reports_its_outcome(
    monkeypatch, patched_modal, tmp_path, capsys, finalization_case
):
    """``finalization_case`` is the publication run, the gates and the outcome."""
    publication_run, gates_passed, expected = finalization_case
    store = install_routing_store(patched_modal, None)
    pending = publication_plan(
        monkeypatch,
        None,
        ("6.1.0", "1.1.0", "2.2.0"),
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    output = tmp_path / "github-output"
    output.write_text("existing=kept\n")
    args = [
        "--finalize-publication",
        "--release-environment",
        "beta",
        "--gates-passed",
        gates_passed,
        "--github-output",
        str(output),
    ]
    if publication_run is not None:
        args.extend(["--publication-run", publication_run])
    # Recovery must display the full pending record before the first write.
    original = registry.finalize_routing_publication

    def finalizer(**kwargs):
        printed = capsys.readouterr().out
        assert printed.startswith("Pending routing publication:\n")
        assert json.loads(printed.partition("\n")[2]) == {
            "status": "pending",
            "plan": pending,
        }
        return original(**kwargs)

    monkeypatch.setattr(registry, "finalize_routing_publication", finalizer)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    run_registry_main(monkeypatch, *args)
    assert output.read_text() == f"existing=kept\noutcome={expected}\n"
    printed = capsys.readouterr().out
    assert f"Routing publication finalization: {expected}" in printed
    if expected == "foreign-publication":
        assert "belongs to another run or environment" in printed
        assert store.get("publication") == {"status": "pending", "plan": pending}
        assert store.get("active") == pending["after"]
    else:
        assert store.get("publication") == {"status": expected, "plan": pending}
        assert store.get("active") == (
            pending["after"] if expected == "completed" else None
        )


def test_cli_missing_journal_is_explicit_and_reports_no_pending(
    monkeypatch, patched_modal, tmp_path, capsys
):
    output = tmp_path / "github-output"
    run_registry_main(
        monkeypatch,
        "--finalize-publication",
        "--publication-run",
        "74401",
        "--release-environment",
        "beta",
        "--gates-passed",
        "true",
        "--github-output",
        str(output),
    )
    assert "No pending routing publication" in capsys.readouterr().out
    assert output.read_text() == "outcome=no-pending\n"
    assert patched_modal == {}


def test_cli_output_loss_is_retryable_after_the_journal_is_completed(
    monkeypatch, patched_modal, tmp_path, capsys
):
    pending = publication_plan(
        monkeypatch,
        None,
        ("6.1.0", "1.1.0", "2.2.0"),
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    args = [
        "--finalize-publication",
        "--publication-run",
        "74401",
        "--release-environment",
        "beta",
        "--gates-passed",
        "true",
        "--github-output",
    ]
    with pytest.raises(FileNotFoundError):
        run_registry_main(monkeypatch, *args, str(tmp_path / "missing" / "output"))
    store = patched_modal["staging/simulation-api-routing-state"]
    assert store.get("publication") == {"status": "completed", "plan": pending}
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    run_registry_main(monkeypatch, *args, str(output))
    assert "No pending routing publication" in capsys.readouterr().out
    assert output.read_text() == "outcome=completed\n"
    assert store.get("publication") == {"status": "completed", "plan": pending}


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--release-environment", "beta"),
        ("--gates-passed", "false"),
        ("--release-environment", "beta", "--gates-passed", "true"),
        ("--release-environment", "beta", "--gates-passed", "perhaps"),
    ],
)
def test_cli_finalizer_requires_explicit_safe_inputs(monkeypatch, patched_modal, args):
    with pytest.raises(SystemExit) as exc:
        run_registry_main(monkeypatch, "--finalize-publication", *args)
    assert exc.value.code == 2
    assert patched_modal == {}


def test_pending_plan_can_be_inspected_locally_before_upload(
    monkeypatch, patched_modal, capsys
):
    plan = publication_plan(monkeypatch, None, ("6.1.0", "1.1.0", "2.2.0"))
    registry.record_routing_plan(environment="staging", plan=plan)
    store = patched_modal["staging/simulation-api-routing-state"]
    saved = json.loads(json.dumps(store.snapshot()))
    for variable in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF"):
        monkeypatch.delenv(variable, raising=False)
    run_registry_main(monkeypatch, "--print-pending-plan")
    assert json.loads(capsys.readouterr().out) == plan
    assert store.snapshot() == saved

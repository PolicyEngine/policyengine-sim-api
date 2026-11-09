"""Publish a validated worker to the separate Stage 12 v2 manifest.

A deployment publishes its worker with ``--keep-default`` before its checks,
leaving the manifest's default version alone, then moves the default with
``--move-default`` after them, checks it with ``--verify-plan``, and restores
it with ``--restore-plan`` if a later step fails. This is Stage 12's own
mechanism; the v1 routing publisher is separate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import modal
from policyengine_simulation_contract.stage12_manifest import (
    V1_ROUTING_STATE_NAME,
    V2_VERSION_MANIFEST_NAME,
    V2CountryWorker,
    V2ManifestLoader,
    V2ManifestStore,
    V2VersionManifest,
    V2WorkerValidation,
    V2WorkerVersion,
    add_v2_worker,
    assert_separate_manifest_names,
    manifest_digest,
    move_v2_default,
    publish_v2_manifest,
    read_v2_manifest,
    v2_application_name,
    validate_manifest_has_no_credentials,
    write_v2_manifest,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle


def build_worker(validation_payload: object) -> V2WorkerVersion:
    """Build the installed bundle's v2 worker entry from a validation record.

    Raises ValueError if the record is invalid or the entry carries a
    credential-like key.
    """
    resolved = load_stage12_bundle()
    validation = V2WorkerValidation.model_validate(validation_payload)
    worker = V2WorkerVersion(
        application_name=v2_application_name(resolved.bundle.policyengine_version),
        report_coordinator_callable="coordinate_report",
        countries=tuple(
            V2CountryWorker(
                country=country.country,
                single_simulation_callable=(f"run_single_simulation_{country.country}"),
            )
            for country in resolved.bundle.countries
        ),
        bundle=resolved.bundle,
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        validation=validation,
        cache=validation.cache,
    )
    validate_manifest_has_no_credentials(worker.model_dump(mode="json"))
    return worker


V2_DEFAULT_PLAN_SCHEMA = "policyengine-simulation.v2-default-plan/v1"


def _manifest_json(manifest: V2VersionManifest) -> dict[str, object]:
    return manifest.model_dump(mode="json")


def _print_manifest(manifest: V2VersionManifest) -> None:
    print(
        json.dumps(
            _manifest_json(manifest),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _publish_worker(
    store: V2ManifestStore, validation_file: Path, *, keep_default: bool
) -> V2VersionManifest:
    """Add the validated worker; ``keep_default`` leaves the default version."""
    worker = build_worker(json.loads(validation_file.read_text(encoding="utf-8")))
    if not keep_default:
        return publish_v2_manifest(store=store, worker=worker)
    manifest = add_v2_worker(current=read_v2_manifest(store), worker=worker)
    write_v2_manifest(store=store, manifest=manifest)
    return manifest


def _stored_digest(store: V2ManifestStore) -> str | None:
    manifest = read_v2_manifest(store)
    return None if manifest is None else manifest_digest(manifest)


def _write_and_check(
    store: V2ManifestStore, manifest: V2VersionManifest, expected_digest: str
) -> None:
    write_v2_manifest(store=store, manifest=manifest)
    if _stored_digest(store) != expected_digest:
        msg = "the written v2 manifest reads back differently"
        raise ValueError(msg)


def _move_default(store: V2ManifestStore, version: str, plan_file: Path) -> dict:
    """Plan moving the default to ``version``, keep the plan, then apply it.

    The plan holds the manifest before and after the move with their
    digests; the move is written only while the stored manifest is still the
    plan's starting point.
    """
    before = read_v2_manifest(store)
    if before is None:
        msg = "there is no v2 manifest to move the default of"
        raise ValueError(msg)
    after = move_v2_default(current=before, version=version)
    plan = {
        "schema": V2_DEFAULT_PLAN_SCHEMA,
        "version": version,
        "before": _manifest_json(before),
        "after": _manifest_json(after),
        "before_sha256": manifest_digest(before),
        "after_sha256": manifest_digest(after),
        "advances_default": after.default_version == version,
    }
    plan_file.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    if _stored_digest(store) != plan["before_sha256"]:
        msg = "the v2 manifest changed after the default move was planned"
        raise ValueError(msg)
    _write_and_check(store, after, plan["after_sha256"])
    return plan


def _read_plan(plan_file: Path) -> tuple[dict, V2VersionManifest, V2VersionManifest]:
    """Return a default plan and its manifests, refusing one that does not match."""
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or plan.get("schema") != V2_DEFAULT_PLAN_SCHEMA:
        msg = f"not a {V2_DEFAULT_PLAN_SCHEMA} plan"
        raise ValueError(msg)
    before = V2VersionManifest.model_validate(plan["before"])
    after = V2VersionManifest.model_validate(plan["after"])
    if (manifest_digest(before), manifest_digest(after)) != (
        plan["before_sha256"],
        plan["after_sha256"],
    ):
        msg = "the plan's manifests do not match its digests"
        raise ValueError(msg)
    return plan, before, after


def _verify_plan(store: V2ManifestStore, plan_file: Path) -> str:
    """Return the default a reader resolves, refusing unless it is the plan's."""
    plan, _before, after = _read_plan(plan_file)
    if _stored_digest(store) != plan["after_sha256"]:
        msg = "the stored v2 manifest is not what this default move wrote"
        raise ValueError(msg)
    resolved_version = V2ManifestLoader(store).resolve()[0]
    if resolved_version != after.default_version:
        msg = (
            f"requests that pin no v2 version resolve to {resolved_version!r}, "
            f"not {after.default_version!r}"
        )
        raise ValueError(msg)
    return resolved_version


def _restore_plan(store: V2ManifestStore, plan_file: Path) -> str:
    """Write back the plan's starting manifest while its move is what is stored.

    Returns ``restored``, or ``unchanged`` when the stored manifest is still
    the starting one (the move was never written, or was already undone).
    Any other stored manifest is a conflict, and nothing is written.
    """
    plan, before, _after = _read_plan(plan_file)
    stored = _stored_digest(store)
    if stored == plan["before_sha256"]:
        return "unchanged"
    if stored != plan["after_sha256"]:
        msg = (
            "the v2 manifest is neither what this default move replaced nor "
            "what it wrote; another publish changed it, so nothing was restored"
        )
        raise ValueError(msg)
    _write_and_check(store, before, plan["before_sha256"])
    return "restored"


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish a validated Stage 12 v2 worker manifest",
    )
    parser.add_argument("--environment", required=True)
    parser.add_argument(
        "--manifest-name",
        default=V2_VERSION_MANIFEST_NAME,
        help="Separate Modal Dict used only for the Stage 12 v2 manifest",
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--validation-file",
        type=Path,
        help="Publish the worker this validation names",
    )
    modes.add_argument(
        "--move-default",
        metavar="VERSION",
        help="Move the default to this added worker when it is newer",
    )
    modes.add_argument(
        "--verify-plan",
        metavar="FILE",
        type=Path,
        help="Refuse unless the stored manifest is what this default plan wrote",
    )
    modes.add_argument(
        "--restore-plan",
        metavar="FILE",
        type=Path,
        help="Write back this default plan's starting manifest while its move is live",
    )
    parser.add_argument(
        "--keep-default",
        action="store_true",
        help="With --validation-file: leave the default version as it is",
    )
    parser.add_argument("--force-default", action="store_true")
    parser.add_argument(
        "--plan-out",
        metavar="FILE",
        type=Path,
        help="With --move-default: where to keep the plan",
    )
    return parser


def main() -> None:
    """Run the requested mode against the v2 manifest Dict and report it.

    Refuses a ``--manifest-name`` that is empty or names the v1 routing state.
    """
    parser = _argument_parser()
    args = parser.parse_args()
    if args.keep_default and args.validation_file is None:
        parser.error("--keep-default needs --validation-file")
    if (args.move_default is None) != (args.plan_out is None):
        parser.error("--move-default and --plan-out go together")

    assert_separate_manifest_names(
        v1_name=V1_ROUTING_STATE_NAME,
        v2_name=args.manifest_name,
    )
    store = modal.Dict.from_name(
        args.manifest_name,
        environment_name=args.environment,
        create_if_missing=True,
    )
    if args.validation_file is not None:
        if args.force_default and args.keep_default:
            parser.error("--force-default moves the default; --keep-default keeps it")
        if args.force_default:
            manifest = publish_v2_manifest(
                store=store,
                worker=build_worker(
                    json.loads(args.validation_file.read_text(encoding="utf-8"))
                ),
                force_default=True,
            )
        else:
            manifest = _publish_worker(
                store, args.validation_file, keep_default=args.keep_default
            )
        _print_manifest(manifest)
    elif args.move_default is not None:
        plan = _move_default(store, args.move_default, args.plan_out)
        print(f"before_sha256={plan['before_sha256']}")
        print(f"after_sha256={plan['after_sha256']}")
        print(f"advances_default={'true' if plan['advances_default'] else 'false'}")
    elif args.verify_plan is not None:
        print(f"v2 default version: {_verify_plan(store, args.verify_plan)}")
    else:
        print(f"v2 default restore: {_restore_plan(store, args.restore_plan)}")


if __name__ == "__main__":
    main()

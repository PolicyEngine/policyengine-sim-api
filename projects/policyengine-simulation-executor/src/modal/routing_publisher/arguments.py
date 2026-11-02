"""The routing publisher's command-line options."""

import argparse

from src.modal.routing_publisher.writers import (
    CANONICAL_GITHUB_REPOSITORY,
    SERIALIZED_ROUTING_WORKFLOW_FILES,
)


def routing_argument_parser() -> argparse.ArgumentParser:
    """Describe the routing publisher's modes and their options."""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Publish the active routing state after Modal deployment.\n"
            "The simulation-deploy-main concurrency group serializes routing writes.\n"
            f"Writes require GitHub Actions in {CANONICAL_GITHUB_REPOSITORY}\n"
            "through one of these workflows:\n"
            + "\n".join(sorted(SERIALIZED_ROUTING_WORKFLOW_FILES))
            + "\nRead-only, --dry-run and --plan-out modes are available locally."
        ),
    )
    parser.add_argument(
        "--app-name",
        help="Versioned app name (e.g., policyengine-simulation-py4-10-0)",
    )
    parser.add_argument(
        "--policyengine-version",
        help="policyengine.py package version (e.g., 4.10.0)",
    )
    parser.add_argument(
        "--us-version",
        help="US package version (e.g., 1.459.0)",
    )
    parser.add_argument(
        "--uk-version",
        help="UK package version (e.g., 2.65.9)",
    )
    parser.add_argument(
        "--environment",
        required=True,
        help="Modal environment (staging or main)",
    )
    parser.add_argument(
        "--force-latest",
        action="store_true",
        help=(
            "Overwrite 'latest' even when the supplied version is older than "
            "the currently recorded latest (use for intentional rollbacks)."
        ),
    )
    parser.add_argument(
        "--seed-active-from-legacy",
        action="store_true",
        help=(
            "Create or merge simulation-api-routing-state[active] from the "
            "legacy policyengine/us/uk Modal dicts without publishing a new app."
        ),
    )
    parser.add_argument(
        "--print-latest",
        action="store_true",
        help="Print latest.policyengine (empty when unset) and change nothing.",
    )
    parser.add_argument(
        "--print-identity",
        action="store_true",
        help=(
            "Print what unpinned requests route to (policyengine_version, "
            "app_name, uk_model_version, uk_dataset_sha256) and change nothing."
        ),
    )
    parser.add_argument(
        "--print-pending-plan",
        action="store_true",
        help="Print the durable unfinished plan as JSON, including before upload.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "With publishing arguments: print a routing plan's digests; change nothing."
        ),
    )
    parser.add_argument(
        "--plan-out",
        metavar="FILE",
        help=(
            "With the publishing arguments: write the routing plan (state "
            "before and after) to FILE and print its digests; change nothing."
        ),
    )
    parser.add_argument(
        "--apply-plan",
        metavar="FILE",
        help="Publish a plan, refusing unless the live state is its starting point.",
    )
    parser.add_argument(
        "--record-plan",
        metavar="FILE",
        help="Record a pending release before uploading its plan or publishing.",
    )
    parser.add_argument(
        "--complete-plan",
        metavar="FILE",
        help="Complete a recorded publication after all release gates pass.",
    )
    parser.add_argument(
        "--finalize-publication",
        action="store_true",
        help="Complete or restore the durable pending publication without an artifact.",
    )
    parser.add_argument(
        "--publication-run",
        help="With --finalize-publication: owning GitHub run id, ignoring its attempt.",
    )
    parser.add_argument(
        "--release-environment",
        choices=("beta", "prod"),
        help="With --finalize-publication: publication's release environment.",
    )
    parser.add_argument(
        "--gates-passed",
        choices=("true", "false"),
        help="With --finalize-publication: whether every release gate passed.",
    )
    parser.add_argument(
        "--github-output",
        metavar="FILE",
        help="With --finalize-publication: append outcome to a GitHub output file.",
    )
    parser.add_argument(
        "--restore-plan",
        metavar="FILE",
        nargs="+",
        help=(
            "Undo a deployment's plans (one per run attempt): restore each "
            "starting state while the live state is what that plan published."
        ),
    )
    parser.add_argument(
        "--publication-id",
        help="With --plan-out or --dry-run: a unique publication id (default random).",
    )
    parser.add_argument(
        "--restore-latest",
        metavar="POLICYENGINE_VERSION",
        help=(
            "Point latest back at this already-published policyengine.py "
            "version instead of publishing a new app."
        ),
    )
    parser.add_argument(
        "--expected-latest",
        metavar="POLICYENGINE_VERSION",
        help="With --restore-latest: refuse unless latest is still this version.",
    )
    return parser

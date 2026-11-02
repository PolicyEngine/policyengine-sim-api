"""Allow routing writes only from the workflows that serialize them."""

import os

CANONICAL_GITHUB_REPOSITORY = "PolicyEngine/policyengine-sim-api"
SERIALIZED_ROUTING_WORKFLOW_FILES = frozenset(
    {
        ".github/workflows/simulation-deploy.yml",
        ".github/workflows/stage12-deploy.yml",
        ".github/workflows/simulation-rollback.yml",
    }
)


def require_serialized_routing_workflow() -> None:
    """Refuse routing writes outside the workflows that serialize them."""
    # GitHub associates a reusable workflow's github context with its caller,
    # so GITHUB_WORKFLOW_REF identifies the caller, not the reusable file:
    # https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#github-context
    # GITHUB_WORKFLOW_REF is owner/repo/path@ref:
    # https://docs.github.com/en/actions/reference/workflows-and-actions/variables
    workflow_path, separator, ref = os.environ.get("GITHUB_WORKFLOW_REF", "").partition(
        "@"
    )
    repository_prefix = f"{CANONICAL_GITHUB_REPOSITORY}/"
    workflow_file = (
        workflow_path.removeprefix(repository_prefix)
        if workflow_path.startswith(repository_prefix)
        else None
    )
    if (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == CANONICAL_GITHUB_REPOSITORY
        and workflow_file in SERIALIZED_ROUTING_WORKFLOW_FILES
        and separator
        and ref
        and "@" not in ref
        and not any(character.isspace() for character in ref)
    ):
        return
    workflows = ", ".join(sorted(SERIALIZED_ROUTING_WORKFLOW_FILES))
    msg = (
        "Routing writes require GitHub Actions in "
        f"{CANONICAL_GITHUB_REPOSITORY} through {workflows}. "
        "Use those workflows, whose simulation-deploy-main concurrency group "
        "serializes routing writes. Read-only, --dry-run and --plan-out modes "
        "are available locally."
    )
    raise RuntimeError(msg)

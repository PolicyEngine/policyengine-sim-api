"""Write the worker's Google Cloud credentials from its environment."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from typing import TYPE_CHECKING

from policyengine_simulation_observability.stages import ANNUAL_IMPACT_STAGES, Stage

if TYPE_CHECKING:
    from collections.abc import Iterator

    from policyengine_observability import ObservabilityRuntime

# Moved out of simulation_runtime.py; it still logs under that module's name.
logger = logging.getLogger("policyengine_simulation_executor.simulation_runtime")

# Credential-adjacent environment values are cut to this many characters.
_ENV_VALUE_PREVIEW_CHARS = 50


def _normalize_credentials_blob(creds_json: str) -> str:
    """Return the raw JSON blob, decoding the outer escape if present.

    The upstream Modal secret sometimes stores the credentials payload
    double-encoded (the entire JSON object is wrapped in quotes with
    backslash-escaped interior quotes). Historically we always attempted
    the unescape as a fallback which could accidentally parse an already
    clean blob. Only unwrap when the payload looks wrapped.
    """
    try:
        json.loads(creds_json)
    except json.JSONDecodeError:
        looks_escaped = creds_json.lstrip().startswith('"') or '\\"' in creds_json
        if looks_escaped:
            return json.loads(f'"{creds_json}"')
        raise
    return creds_json


@contextlib.contextmanager
def setup_gcp_credentials(
    runtime: ObservabilityRuntime | None = None,
) -> Iterator[None]:
    """Set up GCP credentials from environment variable.

    Modal secrets are injected as environment variables. The GCP library
    expects GOOGLE_APPLICATION_CREDENTIALS to point to a file path. If
    credentials JSON is provided, write it to a temp file that's deleted
    on exit. This runs as a context manager to guarantee cleanup even if
    the caller raises mid-simulation; the previous fire-and-forget
    ``tempfile.mkstemp`` path leaked credential material on disk every
    time a container served a request.
    """
    previous = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    creds_file = None
    with contextlib.ExitStack() as credentials_file_stack:
        try:
            credential_span = (
                runtime.span(ANNUAL_IMPACT_STAGES.name(Stage.CREDENTIAL_SETUP))
                if runtime is not None
                else contextlib.nullcontext()
            )
            with credential_span:
                # Log available GCP-related env vars for debugging.
                gcp_vars = {
                    k: v[:_ENV_VALUE_PREVIEW_CHARS] + "..."
                    if len(v) > _ENV_VALUE_PREVIEW_CHARS
                    else v
                    for k, v in os.environ.items()
                    if "GOOGLE" in k or "GCP" in k or "CREDENTIAL" in k
                }
                logger.info("GCP-related env vars: %s", list(gcp_vars.keys()))

                if previous:
                    logger.info("GOOGLE_APPLICATION_CREDENTIALS already set")
                else:
                    creds_json = (
                        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
                        or os.environ.get("GCP_CREDENTIALS_JSON")
                        or os.environ.get("GOOGLE_CREDENTIALS")
                        or os.environ.get("SERVICE_ACCOUNT_JSON")
                    )

                    if not creds_json:
                        logger.warning(
                            "No GCP credentials found in environment variables"
                        )
                    else:
                        normalized = _normalize_credentials_blob(creds_json)
                        # The exit stack keeps the file open across the yield,
                        # and closing it (``delete=True``) removes the file.
                        # The finally block first restores any prior value of
                        # ``GOOGLE_APPLICATION_CREDENTIALS`` so a retry in the same
                        # container doesn't silently pick up a stale path.
                        creds_file = credentials_file_stack.enter_context(
                            tempfile.NamedTemporaryFile(
                                mode="w",
                                suffix=".json",
                                delete=True,
                            )
                        )
                        creds_file.write(normalized)
                        creds_file.flush()
                        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_file.name
                        logger.info("GCP credentials written to %s", creds_file.name)

            yield
        finally:
            if creds_file is not None:
                if previous is None:
                    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
                else:
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = previous

"""Record which environment variables code reads, for the snapshot warm-up tests.

Nothing may read a Modal Secret before Modal takes a worker's memory snapshot,
so the run_simulation and v2 warm-up tests replace ``os.environ`` with
``EnvironmentReadRecorder`` and compare the names read with
``SECRET_ENVIRONMENT_NAMES``.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping

# Modal Secrets the workers have: Google Cloud credentials and the Hugging
# Face token, under every name the code reads them by.
SECRET_ENVIRONMENT_NAMES = {
    "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    "GCP_CREDENTIALS_JSON",
    "GOOGLE_CREDENTIALS",
    "SERVICE_ACCOUNT_JSON",
    "HUGGING_FACE_TOKEN",
    "HF_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
}


class EnvironmentReadRecorder(MutableMapping):
    """Wraps ``os.environ`` and records the name of every variable read."""

    def __init__(self, environment: MutableMapping) -> None:
        self.environment = environment
        self.names_read: set[str] = set()

    def __getitem__(self, name: str) -> str:
        self.names_read.add(name)
        return self.environment[name]

    def __setitem__(self, name: str, value: str) -> None:
        self.environment[name] = value

    def __delitem__(self, name: str) -> None:
        del self.environment[name]

    def __iter__(self) -> Iterator[str]:
        # Listing the variables reads them all, as the credential setup's
        # log of Google-related variables does.
        for name in list(self.environment):
            self.names_read.add(name)
            yield name

    def __len__(self) -> int:
        return len(self.environment)

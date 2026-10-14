"""Shared in-memory stand-in for ``ArtifactStore``'s byte interface.

The Stage 12 artifact tests and the Stage 12 runtime tests both drive the real
``Stage12ArtifactStore`` over this fake, so its write-once behaviour has one
definition instead of a per-file copy.
"""


class InMemoryObjectStore:
    """Keep uploaded objects by path, refusing to replace an existing one."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_bytes(self, path, payload, *, content_type):
        # The real store uploads with ``if_generation_match=0``: an existing
        # object is left in place and the call reports that it created nothing.
        if path in self.objects:
            return False
        self.objects[path] = payload
        return True

    def read_bytes(self, path):
        return self.objects.get(path)

"""Shared in-memory stand-in for ``ArtifactStore``'s byte interface.

The Stage 12 artifact tests and the Stage 12 runtime tests both drive the real
``Stage12ArtifactStore`` over this fake, so its write-once behaviour has one
definition instead of a per-file copy.
"""


class InMemoryObjectStore:
    """Keep uploaded objects by path, refusing to replace an existing one."""

    def __init__(self) -> None:
        """Start with no stored objects."""
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}

    def upload_bytes(self, path: str, payload: bytes, *, content_type: str) -> bool:
        """Store ``payload`` and its content type at ``path``.

        Report whether this call created the object.
        """
        # The real store uploads with ``if_generation_match=0``: an existing
        # object is left in place and the call reports that it created nothing.
        if path in self.objects:
            return False
        self.objects[path] = payload
        self.content_types[path] = content_type
        return True

    def read_bytes(self, path: str) -> bytes | None:
        """Return the bytes stored at ``path``, or None when nothing is there."""
        return self.objects.get(path)

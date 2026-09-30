"""PolicyEngine 6's SPM-aware ``storage_id`` expression for test doubles.

Precompute plans a store path from ``BaselineArtifactIdentity.storage_id``
and the in-container worker aborts when the wrapper's value disagrees, so the
two derivations have to be one identifier reached down two paths.

This is that second path, copied from the installed PolicyEngine 6 wrapper so
the key-discipline and precompute tests state it once. It is an independent
expression of the identifier, not a replacement for tests against the installed
wrapper; ``test_artifact_keys`` compares both paths directly.

``policyengine/core/simulation.py``::

    @property
    def storage_id(self) -> str:
        \"\"\"Include resolved SPM settings in cache and saved-result identity.\"\"\"
        config = self.spm_config
        if config is None:
            return self.id
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        return f"{self.id}-spm-{hashlib.sha256(encoded).hexdigest()}"

``spm_config`` above is not a stored value. On PolicyEngine 6 it
refuses a non-US ``tax_benefit_model_version`` and otherwise re-resolves
``spm`` through the installed bundle, so an unset selection becomes the
bundle's defaults rather than None -- which is why this function takes a
config that has already been resolved, and why the agreement claim is
about the digest, not about the resolution.
"""

import hashlib
import json


def wrapper_storage_id(simulation_id: str, spm_config: dict | None) -> str:
    if spm_config is None:
        return simulation_id
    encoded = json.dumps(spm_config, sort_keys=True, separators=(",", ":")).encode()
    return f"{simulation_id}-spm-{hashlib.sha256(encoded).hexdigest()}"

"""The SPM-capable wrapper's ``storage_id``, transcribed for hermetic tests.

Precompute plans a store path from ``BaselineArtifactIdentity.storage_id``
and the in-container worker aborts when the wrapper's value disagrees, so the
two derivations have to be one identifier reached down two paths.

This is that second path, copied out of the SPM-capable wrapper so the
key-discipline tests and the precompute writer==reader tests state it once.
It is an independent expression of the identifier, not a replacement for
tests against the installed wrapper.

``policyengine/core/simulation.py``::

    @property
    def storage_id(self) -> str:
        \"\"\"Include resolved SPM settings in cache and saved-result identity.\"\"\"
        config = self.spm_config
        if config is None:
            return self.id
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        return f"{self.id}-spm-{hashlib.sha256(encoded).hexdigest()}"

Read from ``policyengine-5.3.0-py3-none-any.whl`` sha256
``8c640d967575dddad70840bcbe938cea251c56958eded647c83eb9c5902735f1``, the
unpublished development wheel the native qualification lane installs. The
hash is the identifier, not the version: another local build carries the
same filename and version and has no ``storage_id`` at all.

``spm_config`` above is not a stored value. On the canonical wrapper it
refuses a non-US ``tax_benefit_model_version`` and otherwise re-resolves
``spm`` through the installed bundle, so an unset selection becomes the
bundle's defaults rather than None -- which is why this function takes a
config that has already been resolved, and why the agreement claim is
about the digest, not about the resolution. Both are checked against that
wheel in ``TestWrapperStorageIdAgreement``'s recorded out-of-band run.
"""

import hashlib
import json


def wrapper_storage_id(simulation_id: str, spm_config: dict | None) -> str:
    if spm_config is None:
        return simulation_id
    encoded = json.dumps(spm_config, sort_keys=True, separators=(",", ":")).encode()
    return f"{simulation_id}-spm-{hashlib.sha256(encoded).hexdigest()}"


def installed_wrapper_has_storage_id() -> bool:
    """Whether the installed wrapper exposes the SPM-aware identifier."""
    from policyengine.core import Simulation

    return hasattr(Simulation, "storage_id")

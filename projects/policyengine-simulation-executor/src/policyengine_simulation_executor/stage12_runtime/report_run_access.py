"""The Modal worker role's database credentials, which report runs are recorded with.

An operator creates the Modal secret in each Modal environment. The v2 Modal
application attaches it to the coordinator and the simulation workers, which
record the status and results of the report run and simulation runs their
call names.
"""

from __future__ import annotations

# The Modal secret and the one key it must hold: a postgresql:// URL for the
# Modal worker role on the database that holds the Public API's run tables.
MODAL_WORKER_DATABASE_SECRET_NAME = "policyengine-modal-worker-database"  # noqa: S105  # the Modal secret's name, not its value
MODAL_WORKER_DATABASE_URL_ENV = "MODAL_WORKER_DATABASE_URL"

"""Refuse to deploy or run an app with snapshots from a Modal client before 1.6.4.

Containers restored from a memory snapshot reload Secret values only from
Modal 1.6.4, and each app's containers run the Modal client that deployed it.
This project's uv.lock pins such a client; the check refuses a deploy or run
from an environment with an older one, such as a global Modal install. The
run_simulation app (``src/modal/app.py``) and the v2 app
(``src/modal/v2_app.py``) both run it on import, on the deploying machine,
before they read anything.
"""

from packaging.version import InvalidVersion, Version

FIRST_MODAL_CLIENT_RELOADING_SECRETS_ON_RESTORE = Version("1.6.4")


def require_secret_reloading_modal_client(
    client_version: str, *, app_path: str
) -> None:
    """Raise unless ``client_version`` is Modal 1.6.4 or newer.

    Versions compare as Python packaging defines them, so 1.6.4's development
    builds and release candidates count as older than 1.6.4. ``app_path`` is
    the app's file, relative to the executor project, named in the message.
    """
    try:
        reloads_secrets = (
            Version(client_version) >= FIRST_MODAL_CLIENT_RELOADING_SECRETS_ON_RESTORE
        )
    except InvalidVersion:
        reloads_secrets = False
    if not reloads_secrets:
        msg = (
            f"Modal client {client_version} is not "
            f"{FIRST_MODAL_CLIENT_RELOADING_SECRETS_ON_RESTORE} or newer: containers "
            "restored from this app's memory snapshots would keep the Secret values "
            "from when each snapshot was taken. Deploy and run from this project's "
            "locked environment, for example "
            f"`uv run --frozen modal deploy {app_path}`."
        )
        raise RuntimeError(msg)

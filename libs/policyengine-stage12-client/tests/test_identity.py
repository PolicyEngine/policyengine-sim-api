from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from policyengine_stage12_client.identity import (
    _expiry_is_near,
    _service_account_info,
)


def test_service_account_info_accepts_modal_json_secret() -> None:
    payload = {"type": "service_account", "client_email": "runtime@example.test"}

    assert (
        _service_account_info(
            {"GOOGLE_APPLICATION_CREDENTIALS_JSON": json.dumps(payload)}
        )
        == payload
    )


def test_expiry_check_accepts_google_auth_naive_utc_timestamp() -> None:
    now = datetime.now(UTC)

    assert _expiry_is_near((now + timedelta(seconds=30)).replace(tzinfo=None))
    assert not _expiry_is_near((now + timedelta(minutes=10)).replace(tzinfo=None))

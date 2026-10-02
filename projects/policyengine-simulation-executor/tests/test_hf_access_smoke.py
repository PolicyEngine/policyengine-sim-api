"""Declaration tests for the live Modal credential access check."""

from __future__ import annotations

import importlib
import sys

from fixtures.fake_modal import install_fake_modal


def test_access_check_uses_only_the_purpose_specific_secret(monkeypatch) -> None:
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.hf_access_smoke", None)

    module = importlib.import_module("src.modal.hf_access_smoke")

    assert module.app.name == "pe-uk-private-hf-read-access-check"
    assert module.hf_secret == {
        "args": ("pe-uk-private-hf-read-token",),
        "kwargs": {"required_keys": ["HUGGING_FACE_TOKEN"]},
    }
    verify_call = next(
        kwargs
        for function_name, kwargs in module.app.function_calls
        if function_name == "verify_access"
    )
    assert verify_call["secrets"] == [module.hf_secret]
    assert verify_call["timeout"] == 120

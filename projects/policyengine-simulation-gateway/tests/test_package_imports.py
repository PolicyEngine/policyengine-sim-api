import sys
import importlib


def test_gateway_models_import_does_not_import_fastapi_endpoints(
    import_gateway_models,
    gateway_import_module_names,
):
    import_gateway_models()

    assert gateway_import_module_names.endpoints not in sys.modules
    assert gateway_import_module_names.fastapi not in sys.modules


def test_gateway_endpoints_import_does_not_import_policyengine_bundle(
    isolated_gateway_model_import_modules,
    gateway_import_module_names,
):
    release_bundle_module = sys.modules.pop(
        "policyengine_simulation_executor.release_bundle", None
    )

    try:
        importlib.import_module(gateway_import_module_names.endpoints)

        assert "policyengine_simulation_executor.release_bundle" not in sys.modules
    finally:
        if release_bundle_module is not None:
            sys.modules["policyengine_simulation_executor.release_bundle"] = (
                release_bundle_module
            )

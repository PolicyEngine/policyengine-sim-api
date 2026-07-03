"""Root pytest configuration for simulation API tests."""

import importlib
import sys
from pathlib import Path

pytest_plugins = (
    "fixtures.gateway.shared",
    "fixtures.gateway.test_endpoints",
    "fixtures.gateway.package_imports",
)

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

ensure_project_root_on_path = importlib.import_module(
    "fixtures.test_support"
).ensure_project_root_on_path

ensure_project_root_on_path()

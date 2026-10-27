#!/usr/bin/env bash
# Install uv with its install script and as a Python package, both without a
# release, so each installs the newest one.
set -euo pipefail

curl -LsSf https://astral.sh/uv/install.sh | sh
pip install uv

#!/usr/bin/env bash
# Install the uv release in .uv-version with its install script and as a
# Python package.
set -euo pipefail

curl -LsSf https://astral.sh/uv/0.13.6/install.sh | sh
pip install "uv==0.13.6"

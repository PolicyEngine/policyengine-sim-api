#!/bin/bash
# Ensure Modal environments exist
# Usage: ./modal-setup-environments.sh

set -euo pipefail

# Create staging environment if it doesn't exist
uv run --frozen modal environment create staging 2>/dev/null || echo "staging environment already exists"

# main environment exists by default
echo "Modal environments ready"

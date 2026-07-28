#!/bin/bash
# Generate deployment summary for GitHub Actions
# Usage: ./simulation-deployment-summary.sh <beta-result> <beta-candidate-url> <prod-result> <prod-stable-url>

set -euo pipefail

BETA_RESULT="${1:-skipped}"
BETA_URL="${2:-}"
PROD_RESULT="${3:-skipped}"
PROD_URL="${4:-}"

{
  echo "## Simulation API Deployment Summary"
  echo ""

  case "$BETA_RESULT" in
    success)
      echo "✅ **Beta deployment**: Success"
      [ -n "$BETA_URL" ] && echo "   - Candidate URL: $BETA_URL"
      ;;
    skipped)
      echo "⏭️ **Beta deployment**: Skipped"
      ;;
    *)
      echo "❌ **Beta deployment**: $BETA_RESULT"
      ;;
  esac

  echo ""

  case "$PROD_RESULT" in
    success)
      echo "✅ **Prod deployment**: Success"
      [ -n "$PROD_URL" ] && echo "   - Stable URL: $PROD_URL"
      ;;
    skipped)
      echo "⏭️ **Prod deployment**: Skipped"
      ;;
    *)
      echo "❌ **Prod deployment**: $PROD_RESULT"
      ;;
  esac
} >> "$GITHUB_STEP_SUMMARY"

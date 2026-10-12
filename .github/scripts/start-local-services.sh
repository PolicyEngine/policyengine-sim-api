#!/usr/bin/env bash
# Start the local Docker Compose services and wait for them to answer.
# Usage: start-local-services.sh <compose-file>
#
# Waits 10 seconds, then polls the local gateway's liveness endpoint up to 30
# times, 2 seconds apart. It does not fail when the services never answer: the
# integration tests that follow report that.

set -euo pipefail

COMPOSE_FILE="${1:?Docker Compose file required}"

docker compose -f "$COMPOSE_FILE" up -d
# Wait for services to be ready
sleep 10
# Check services are responding
for i in {1..30}; do
  if curl -f http://localhost:8082/ping/alive; then
    echo "Services are ready!"
    break
  fi
  echo "Waiting for services... (attempt $i/30)"
  sleep 2
done

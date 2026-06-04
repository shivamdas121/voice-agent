#!/usr/bin/env bash
# One-paste deploy: builds & starts the voice agent + trigger server, then connects them to n8n
# so n8n can reach the trigger server privately at http://trigger-server:8080.
# Run on the VPS:  cd /opt/voice-agent && sed -i 's/\r$//' deploy.sh && bash deploy.sh
set -e
cd "$(dirname "$0")"

# Use whichever compose command exists (Docker Compose v2 or v1).
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "ERROR: Docker Compose not found on this VPS."
  exit 1
fi

echo "==> Building and starting containers (this can take a few minutes the first time)..."
$DC up -d --build

echo "==> Locating the trigger-server container and its network..."
TRIGGER_CONTAINER=$($DC ps -q trigger-server)
if [ -z "$TRIGGER_CONTAINER" ]; then
  echo "ERROR: trigger-server container not found."
  $DC ps
  exit 1
fi
OUR_NET=$(docker inspect "$TRIGGER_CONTAINER" -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
echo "    trigger-server is on network: $OUR_NET"

echo "==> Auto-detecting the n8n container..."
N8N_CONTAINER=$(docker ps --format '{{.Names}} {{.Image}}' | grep -i n8n | awk '{print $1}' | head -n1)
if [ -z "$N8N_CONTAINER" ]; then
  echo "    WARNING: could not auto-detect an n8n container. Running containers:"
  docker ps --format '      {{.Names}}   ({{.Image}})'
  echo "    Fix manually with:  docker network connect $OUR_NET <your-n8n-container-name>"
else
  if docker network connect "$OUR_NET" "$N8N_CONTAINER" 2>/dev/null; then
    echo "    Connected n8n container '$N8N_CONTAINER' to '$OUR_NET'."
  else
    echo "    n8n container '$N8N_CONTAINER' was already connected (or connect skipped) — fine."
  fi
fi

echo ""
echo "==> Current status:"
$DC ps
echo ""
echo "============================================================"
echo " DONE."
echo " In n8n, set the HTTP Request URL to:"
echo "     http://trigger-server:8080/lead-webhook"
echo " Detected n8n container: ${N8N_CONTAINER:-<none found>}"
echo "============================================================"

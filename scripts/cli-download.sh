#!/bin/bash
# Trigger a full download via the app API (stack must be running).
set -euo pipefail

APP_URL="${APP_URL:-http://localhost:8000}"
VERSION="${1:-}"

body='{"push_images":true,"save_image_tars":true}'
if [ -n "$VERSION" ]; then
  body=$(printf '{"kubespray_version":"%s","push_images":true,"save_image_tars":true}' "$VERSION")
fi

curl -fsS -X POST "${APP_URL}/api/download" \
  -H 'Content-Type: application/json' \
  -d "$body"
echo
echo "Tail status with: curl -s ${APP_URL}/api/status | jq"

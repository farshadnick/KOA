#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p data/outputs data/cache data/logs

if [ ! -f config/v2ray.json ]; then
  cp config/v2ray.json.example config/v2ray.json
  echo "Created config/v2ray.json from example (direct/freedom outbound)."
  echo "Replace it with your real V2Ray/VMess/VLESS config, then: docker compose restart v2ray"
else
  echo "config/v2ray.json already exists"
fi

echo "Ready. Next: docker compose up -d --build"

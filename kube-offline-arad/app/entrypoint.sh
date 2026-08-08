#!/bin/bash
set -euo pipefail

mkdir -p "${OUTPUTS_DIR:-/data/outputs}"/{files,images,charts,kubespray,scripts,pypi,debs,rpms}
mkdir -p "${CACHE_DIR:-/data/cache}"
mkdir -p /data/logs

mkdir -p /etc/containers
cat >/etc/containers/registries.conf <<EOF
unqualified-search-registries = ["docker.io"]

[[registry]]
location = "registry:5000"
insecure = true

[[registry]]
location = "${REGISTRY_PUBLIC_HOST:-localhost:35000}"
insecure = true

[[registry]]
location = "hub.aradarpanet.ir"
insecure = true
EOF

exec uvicorn server:app --host 0.0.0.0 --port 8000 --log-level info

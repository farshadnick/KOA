#!/bin/bash
# Start a local Docker registry for offline Kubespray (same idea as kubespray-offline).
# https://github.com/kubespray-offline/kubespray-offline/blob/develop/target-scripts/start-registry.sh

set -euo pipefail
cd "$(dirname "$0")"
source ./config.sh

REGISTRY_IMAGE=registry:${REGISTRY_VERSION}
REGISTRY_DIR=${REGISTRY_DIR:-/var/lib/registry}

if [ ! -e "$REGISTRY_DIR" ]; then
  sudo mkdir -p "$REGISTRY_DIR"
fi

echo "===> Stop registry"
$CTR container update --restart=no registry 2>/dev/null || true
$CTR stop registry 2>/dev/null || true
$CTR rm registry 2>/dev/null || true

echo "===> Start registry on :${REGISTRY_PORT}"
$CTR run -d \
  --network host \
  -e REGISTRY_HTTP_ADDR=0.0.0.0:${REGISTRY_PORT} \
  --restart always \
  --name registry \
  -v "${REGISTRY_DIR}:/var/lib/registry" \
  "${REGISTRY_IMAGE}"

echo "Registry listening on 0.0.0.0:${REGISTRY_PORT}"

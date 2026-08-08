#!/bin/bash
# Copied into outputs/scripts for use on the air-gapped / bastion host.

REGISTRY_VERSION=${REGISTRY_VERSION:-2.8.3}
NGINX_VERSION=${NGINX_VERSION:-1.27-alpine}
REGISTRY_PORT=${REGISTRY_PORT:-35000}
HTTP_PORT=${HTTP_PORT:-8080}
REGISTRY_DIR=${REGISTRY_DIR:-/var/lib/registry}

# Prefer nerdctl when present (containerd), else docker
if command -v nerdctl >/dev/null 2>&1; then
  CTR="${CTR:-sudo nerdctl --namespace default}"
elif command -v docker >/dev/null 2>&1; then
  CTR="${CTR:-docker}"
else
  echo "Neither nerdctl nor docker found" >&2
  exit 1
fi

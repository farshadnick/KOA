#!/bin/bash
# Load docker-archive tarballs from ../images and push into the local registry.

set -euo pipefail
cd "$(dirname "$0")"
source ./config.sh

IMAGES_DIR="$(cd ../images && pwd)"
LIST="${IMAGES_DIR}/images.list"
REGISTRY_HOST="${REGISTRY_HOST:-127.0.0.1:${REGISTRY_PORT}}"

if [ ! -f "$LIST" ]; then
  echo "Missing ${LIST}" >&2
  exit 1
fi

if ! command -v skopeo >/dev/null 2>&1; then
  echo "skopeo is required on the target host to push archives" >&2
  exit 1
fi

safe_name() {
  echo "$1" | sed 's/[^a-zA-Z0-9._-]/_/g'
}

normalize() {
  local ref="$1"
  if [[ "$ref" != */* ]]; then
    echo "docker.io/library/${ref}"
  elif [[ "$ref" != *.*/* ]] && [[ "$ref" != */*/* ]]; then
    # user/image without registry
    echo "docker.io/${ref}"
  else
    echo "$ref"
  fi
}

registry_path() {
  local ref
  ref="$(normalize "$1")"
  ref="${ref#http://}"
  ref="${ref#https://}"
  local first="${ref%%/*}"
  if [[ "$first" == *.* ]] || [[ "$first" == *:* ]] || [[ "$first" == localhost ]]; then
    echo "${ref#*/}"
  else
    echo "$ref"
  fi
}

while IFS= read -r line || [ -n "$line" ]; do
  line="$(echo "$line" | sed 's/#.*//' | xargs)"
  [ -z "$line" ] && continue
  path="$(registry_path "$line")"
  tar="${IMAGES_DIR}/$(safe_name "$(normalize "$line")").tar"
  dest="docker://${REGISTRY_HOST}/${path}"
  if [ -f "$tar" ]; then
    echo "===> Push ${tar##*/} -> ${dest}"
    skopeo copy --insecure-policy --dest-tls-verify=false \
      "docker-archive:${tar}:$(normalize "$line")" "${dest}"
  else
    echo "===> WARN missing archive for ${line}"
  fi
done < "$LIST"

echo "Done."

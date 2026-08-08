#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p kubespray-src
shopt -s nullglob
tars=(kubespray/kubespray-*.tar.gz)
if [ ${#tars[@]} -eq 0 ]; then
  echo "No kubespray tarball under ./kubespray/" >&2
  exit 1
fi
tar -xzf "${tars[0]}" -C kubespray-src --strip-components=0
echo "Extracted to $(pwd)/kubespray-src"
ls -1 kubespray-src

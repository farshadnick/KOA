#!/bin/bash
# Serve downloaded files/ (and charts/) for Kubespray offline file URLs.

set -euo pipefail
cd "$(dirname "$0")"
source ./config.sh

BASEDIR="$(cd .. && pwd)"
NGINX_IMAGE=nginx:${NGINX_VERSION}

if [ ! -d "${BASEDIR}/files" ]; then
  echo "Expected ${BASEDIR}/files — run from outputs/scripts after a download job" >&2
  exit 1
fi

echo "===> Stop nginx"
$CTR container update --restart=no nginx 2>/dev/null || true
$CTR stop nginx 2>/dev/null || true
$CTR rm nginx 2>/dev/null || true

echo "===> Start nginx on :${HTTP_PORT}"
$CTR run -d \
  --network host \
  --restart always \
  --name nginx \
  -v "${BASEDIR}:/usr/share/nginx/html:ro" \
  -e NGINX_PORT="${HTTP_PORT}" \
  "${NGINX_IMAGE}" \
  sh -c "sed -i 's/listen       80;/listen       ${HTTP_PORT};/' /etc/nginx/conf.d/default.conf && nginx -g 'daemon off;'"

echo "HTTP file server: http://0.0.0.0:${HTTP_PORT}/"

#!/bin/bash
# Run inside ubuntu:22.04/24.04 to mirror apt packages for offline nodes.
# Mirrors kubespray-offline create-repo-ubuntu.sh behavior.
set -euo pipefail

umask 022
OUTPUTS="${OUTPUTS_DIR:-/data/outputs}"
PKGLIST_DIR="${PKGLIST_DIR:-/pkglist}"
CACHEDIR="${CACHE_DIR:-/data/cache}/cache-debs"
VERSION_ID="${VERSION_ID:-22.04}"

export DEBIAN_FRONTEND=noninteractive
export http_proxy="${HTTP_PROXY:-}"
export https_proxy="${HTTPS_PROXY:-}"
export HTTP_PROXY="${HTTP_PROXY:-}"
export HTTPS_PROXY="${HTTPS_PROXY:-}"

apt-get update
apt-get install -y --no-install-recommends \
  apt-transport-https ca-certificates curl gnupg lsb-release apt-utils dpkg-dev gzip

PKGS_FILE="${PKGLIST_DIR}/ubuntu/pkgs.txt"
EXTRA="${PKGLIST_DIR}/ubuntu/${VERSION_ID}/pkgs.txt"
PKGS=$(grep -vE '^\s*(#|$)' "$PKGS_FILE" || true)
if [ -f "$EXTRA" ]; then
  PKGS=$(printf '%s\n%s\n' "$PKGS" "$(grep -vE '^\s*(#|$)' "$EXTRA" || true)")
fi
PKGS=$(echo "$PKGS" | sort -u | tr '\n' ' ')

mkdir -p "$CACHEDIR"
echo "===> Resolving dependencies for: $PKGS"
# shellcheck disable=SC2086
DEPS=$(apt-cache depends --recurse --no-recommends --no-suggests \
  --no-conflicts --no-breaks --no-replaces --no-enhances --no-pre-depends $PKGS \
  | grep -E '^\w' | sort -u | tr '\n' ' ')

echo "===> Downloading packages"
cd "$CACHEDIR"
# shellcheck disable=SC2086
apt-get download $PKGS $DEPS || apt-get download $PKGS

DEBDIR="${OUTPUTS}/debs/local"
rm -rf "$DEBDIR"
mkdir -p "$DEBDIR/pkgs"
cp -a "$CACHEDIR"/*.deb "$DEBDIR/pkgs/" 2>/dev/null || true
rm -f "$DEBDIR/pkgs/"*i386.deb 2>/dev/null || true

pushd "$DEBDIR" >/dev/null
apt-ftparchive packages pkgs > Packages
gzip -c9 Packages > Packages.gz
apt-ftparchive release . > Release
popd >/dev/null

echo "Ubuntu apt repo created at ${DEBDIR}"

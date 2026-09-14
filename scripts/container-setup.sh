#!/bin/sh
# Prepare a pinned Debian-based CPython image for building or testing.
#   build: python:3.12-slim-bookworm; test: python:3.12-slim-trixie (or 3.13)
# The GCC development packages only satisfy CMake compiler probes; pyhdiff
# links the private LLVM runtime instead. Debian packages come from the snapshot that the image itself was built from,
# so a rerun installs the same package versions.
set -eu
role="$1"
sources=/etc/apt/sources.list.d/debian.sources
snapshot="$(sed -n 's|^# http://snapshot.debian.org/archive/debian/\([0-9TZ]*\)$|\1|p' "$sources" | head -n1)"
[ -n "$snapshot" ] || { echo "image does not record its Debian snapshot" >&2; exit 1; }
sed -i -e "s|^URIs: http://deb.debian.org/debian-security$|URIs: http://snapshot.debian.org/archive/debian-security/$snapshot|" \
       -e "s|^URIs: http://deb.debian.org/debian$|URIs: http://snapshot.debian.org/archive/debian/$snapshot|" "$sources"
printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/99snapshot
apt-get update
case "$role" in
  build) packages="binutils git libc6-dev libgcc-12-dev libstdc++-12-dev xz-utils" ;;
  test) packages="zstd" ;;
  *) echo "usage: $0 build|test" >&2; exit 2 ;;
esac
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $packages
python -m pip install --no-cache-dir --require-hashes --only-binary :all: -r "$(dirname "$0")/../requirements/$role.txt"

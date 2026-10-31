#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build, test, install and check the pacman package inside a fresh Arch container.
# CI runs this in archlinux:base-devel; locally:
#   podman run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" docker.io/library/archlinux:base-devel \
#       bash /src/packaging/arch/build.sh /out
# Usage: build.sh OUTPUT_DIR [--tag vX.Y.Z]
set -euo pipefail
[[ $# -ge 1 ]] || { echo 'Usage: build.sh OUTPUT_DIR [--tag vX.Y.Z]' >&2; exit 1; }
src=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
out=$(mkdir -p "$1" && cd "$1" && pwd)
shift
pacman -Syu --noconfirm --needed git python github-cli systemd namcap
stage=$(mktemp -d)
python -I "$src/packaging/arch/prepare.py" "$stage" "$@"
id builder >/dev/null 2>&1 || useradd --create-home builder
chown -R builder:builder "$stage"
# makepkg refuses to run as root; check() runs the offline test suite.
runuser -u builder -- bash -c "cd '$stage' && makepkg --noconfirm"
packages=("$stage"/*.pkg.tar.zst)
[[ ${#packages[@]} == 1 && -f ${packages[0]} ]] || { echo 'Expected exactly one package.' >&2; exit 1; }
pacman -U --noconfirm "${packages[0]}"
bash "$src/packaging/arch/check.sh" "${packages[0]}"
cp "${packages[0]}" "$out/"
(cd "$out" && sha256sum ./*.pkg.tar.zst > SHA256SUMS && cat SHA256SUMS)

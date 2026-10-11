#!/usr/bin/env bash
# Extract libnickel.so.1.0.0 from a Kobo/Tolino 5.x update.tar, without root.
#
# The update members are zstd-compressed; rootfs.img is an ext4 image. Files are read with
# debugfs (e2fsprogs), so nothing needs to be mounted.
#
# Usage: extract-firmware.sh <update.tar> <outdir>
# Needs: tar, zstd (unzstd), debugfs (package e2fsprogs; on Debian/Ubuntu it is in
#        /usr/sbin or /sbin, which may not be on a normal user's PATH).
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <update.tar> <outdir>" >&2
    exit 1
fi
archive="$(realpath -- "$1")"
out="$2"

debugfs_bin="$(command -v debugfs || true)"
for candidate in /usr/sbin/debugfs /sbin/debugfs; do
    [[ -z "${debugfs_bin}" && -x "${candidate}" ]] && debugfs_bin="${candidate}"
done
if [[ -z "${debugfs_bin}" ]]; then
    echo "debugfs not found; install e2fsprogs" >&2
    exit 1
fi
command -v unzstd >/dev/null || { echo "unzstd not found; install zstd" >&2; exit 1; }

mkdir -p "${out}"
tmp="$(mktemp -d)"
trap 'rm -rf -- "${tmp}"' EXIT

if ! tar -tf "${archive}" rootfs.img >/dev/null 2>&1; then
    echo "${archive} has no rootfs.img (recovery-only or not a 5.x update.tar?)" >&2
    exit 1
fi
tar -xOf "${archive}" rootfs.img | unzstd -c > "${tmp}/rootfs.ext4"
"${debugfs_bin}" -R "dump /usr/local/Kobo/libnickel.so.1.0.0 ${out}/libnickel.so.1.0.0" \
    "${tmp}/rootfs.ext4" 2>/dev/null
"${debugfs_bin}" -R "cat /etc/rootfs-branch" "${tmp}/rootfs.ext4" 2>/dev/null \
    > "${out}/rootfs-branch" || true

if [[ ! -s "${out}/libnickel.so.1.0.0" ]]; then
    echo "libnickel.so.1.0.0 not found in rootfs" >&2
    exit 1
fi
echo "Extracted ${out}/libnickel.so.1.0.0 (firmware branch: $(cat "${out}/rootfs-branch" 2>/dev/null || echo unknown))"

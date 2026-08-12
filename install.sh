#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer as root: ./install.sh" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="/opt/proxmox-image-factory"
STATE="/var/lib/proxmox-image-factory"

# This installer must run on a Proxmox VE node.
for cmd in qm pveversion pvesh; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: $cmd was not found; run this installer on a Proxmox VE node." >&2
    exit 1
  fi
done

echo "Detected Proxmox VE:"
pveversion
echo

# PVE provides its own QEMU stack. Do not install Debian's qemu-utils or
# qemu-system-* packages; they may conflict with pve-qemu-kvm and cause APT to
# remove core Proxmox VE packages.
if ! command -v qemu-img >/dev/null 2>&1; then
  echo "ERROR: qemu-img was not found." >&2
  echo "Do not run 'apt install qemu-utils'." >&2
  echo "Check the PVE pve-qemu-kvm package first:" >&2
  echo "  dpkg -l pve-qemu-kvm" >&2
  echo "  apt-cache policy pve-qemu-kvm" >&2
  exit 1
fi

DEPS=(
  python3-yaml
  libguestfs-tools
  ca-certificates
)

echo "[1/6] APT safety check"

# Simulate installation first and ensure no core PVE packages would be removed.
SIM_OUT="$(apt-get -s install "${DEPS[@]}" 2>&1)" || {
  printf '%s\n' "$SIM_OUT" >&2
  echo "FATAL: apt-get simulation failed; no packages were installed." >&2
  exit 1
}

printf '%s\n' "$SIM_OUT"

PROTECTED_PACKAGES=(
  proxmox-ve
  pve-manager
  pve-qemu-kvm
  qemu-server
  pve-container
  pve-ha-manager
  spiceterm
)

for pkg in "${PROTECTED_PACKAGES[@]}"; do
  if printf '%s\n' "$SIM_OUT" | grep -Eq "^Remv[[:space:]]+${pkg}([[:space:]:]|$)"; then
    echo >&2
    echo "FATAL: APT simulation would remove core Proxmox VE package: $pkg" >&2
    echo "Refusing to continue; no packages were installed." >&2
    exit 2
  fi
done

echo "[2/6] Installing dependencies"
DEBIAN_FRONTEND=noninteractive apt-get install -y "${DEPS[@]}"

echo "[3/6] Verifying dependency commands"
for cmd in qemu-img virt-customize virt-sysprep qm pvesh; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: Command still missing after installation: $cmd" >&2
    exit 1
  fi
done

if ! python3 -c 'import yaml' >/dev/null 2>&1; then
  echo "ERROR: Python 3 cannot import yaml; check the python3-yaml package." >&2
  exit 1
fi

echo "[4/6] Installing Image Factory"
install -d -m 0755 \
  "$DST/bin" \
  "$DST/config" \
  "$STATE/cache" \
  "$STATE/work"

install -m 0755 "$SRC_DIR/bin/build-images.py" "$DST/bin/build-images.py"
install -m 0755 "$SRC_DIR/bin/newvm.py" "$DST/bin/newvm.py"

if [[ ! -f "$DST/config/images.yaml" ]]; then
  install -m 0644 "$SRC_DIR/config/images.yaml" "$DST/config/images.yaml"
else
  echo "Preserving existing configuration: $DST/config/images.yaml"
  install -m 0644 "$SRC_DIR/config/images.yaml" "$DST/config/images.yaml.example"
  echo "Updated configuration example: $DST/config/images.yaml.example"
  echo "Review changes with: diff -u $DST/config/images.yaml $DST/config/images.yaml.example"
fi

ln -sfn "$DST/bin/build-images.py" /usr/local/sbin/pve-image-build
ln -sfn "$DST/bin/newvm.py" /usr/local/sbin/newvm

echo "[5/6] Installing systemd units"
install -m 0644 \
  "$SRC_DIR/systemd/proxmox-image-factory.service" \
  /etc/systemd/system/proxmox-image-factory.service

install -m 0644 \
  "$SRC_DIR/systemd/proxmox-image-factory.timer" \
  /etc/systemd/system/proxmox-image-factory.timer

systemctl daemon-reload

echo "[6/6] Enabling the automatic update timer"
systemctl enable --now proxmox-image-factory.timer

cat <<'EOF'

Installation complete.

Next steps:

1. Edit the configuration:
   nano /opt/proxmox-image-factory/config/images.yaml

2. Verify at minimum:
   global.storage
   global.bridge
   Each image's slot VMIDs are unused

3. Check upstream images first:
   pve-image-build --check

4. Start by building Debian only:
   pve-image-build --only debian-13

5. Build all images after confirming it works:
   pve-image-build --all

6. List available templates:
   newvm --list

EOF

#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "请以 root 运行: ./install.sh" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="/opt/proxmox-image-factory"
STATE="/var/lib/proxmox-image-factory"

# 必须在 Proxmox VE 节点上运行
for cmd in qm pveversion pvesh; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: 找不到 $cmd；请在 Proxmox VE 节点上运行。" >&2
    exit 1
  fi
done

echo "检测到 Proxmox VE:"
pveversion
echo

# PVE 自带自己的 QEMU 栈。不要安装 Debian 的 qemu-utils / qemu-system-*，
# 否则可能与 pve-qemu-kvm 冲突并导致 apt 试图移除 Proxmox VE 核心包。
if ! command -v qemu-img >/dev/null 2>&1; then
  echo "ERROR: 找不到 qemu-img。" >&2
  echo "不要执行 'apt install qemu-utils'。" >&2
  echo "请先检查 PVE 的 pve-qemu-kvm 包状态：" >&2
  echo "  dpkg -l pve-qemu-kvm" >&2
  echo "  apt-cache policy pve-qemu-kvm" >&2
  exit 1
fi

DEPS=(
  python3-yaml
  libguestfs-tools
  ca-certificates
)

echo "[1/6] APT 安全预检"

# 先模拟安装，确保不会删除 PVE 核心包
SIM_OUT="$(apt-get -s install "${DEPS[@]}" 2>&1)" || {
  printf '%s\n' "$SIM_OUT" >&2
  echo "FATAL: apt-get 模拟安装失败，未执行任何真实安装。" >&2
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
    echo "FATAL: APT 模拟显示将移除 Proxmox VE 核心包：$pkg" >&2
    echo "已拒绝继续，未执行真实安装。" >&2
    exit 2
  fi
done

echo "[2/6] 安装依赖"
DEBIAN_FRONTEND=noninteractive apt-get install -y "${DEPS[@]}"

echo "[3/6] 校验依赖命令"
for cmd in qemu-img virt-customize virt-sysprep qm pvesh; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: 安装后仍找不到命令: $cmd" >&2
    exit 1
  fi
done

if ! python3 -c 'import yaml' >/dev/null 2>&1; then
  echo "ERROR: Python 3 无法导入 yaml 模块；请检查 python3-yaml 包状态。" >&2
  exit 1
fi

echo "[4/6] 安装 Image Factory"
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
  echo "保留现有配置: $DST/config/images.yaml"
  install -m 0644 "$SRC_DIR/config/images.yaml" "$DST/config/images.yaml.example"
fi

ln -sfn "$DST/bin/build-images.py" /usr/local/sbin/pve-image-build
ln -sfn "$DST/bin/newvm.py" /usr/local/sbin/newvm

echo "[5/6] 安装 systemd unit"
install -m 0644 \
  "$SRC_DIR/systemd/proxmox-image-factory.service" \
  /etc/systemd/system/proxmox-image-factory.service

install -m 0644 \
  "$SRC_DIR/systemd/proxmox-image-factory.timer" \
  /etc/systemd/system/proxmox-image-factory.timer

systemctl daemon-reload

echo "[6/6] 启用自动更新 timer"
systemctl enable --now proxmox-image-factory.timer

cat <<'EOF'

安装完成。

下一步：

1. 修改配置：
   nano /opt/proxmox-image-factory/config/images.yaml

2. 至少确认：
   global.storage
   global.bridge
   各发行版 slots VMID 未被占用

3. 先检查上游：
   pve-image-build --check

4. 建议先只构建 Debian：
   pve-image-build --only debian-13

5. 确认正常后全部构建：
   pve-image-build --all

6. 查看可用模板：
   newvm --list

EOF

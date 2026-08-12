# Proxmox Image Factory

用于 Proxmox VE 的多发行版 Cloud Image 自动模板流水线。

## 设计

每个发行版分配两个 VMID slot，例如：

- Debian 13: `9000 / 9001`
- Ubuntu 24.04: `9010 / 9011`

A/B 两槽交替更新：

1. 从官方 checksum 判断 upstream 是否变化。
2. 下载并校验官方 cloud image。
3. `virt-customize` 离线安装 `qemu-guest-agent` 和基础包。
4. `virt-sysprep` 清除 machine-id / SSH host keys / 固化 MAC。
5. 把新镜像导入 inactive slot 并转成 Proxmox Template。
6. 从 candidate template 做一个临时 **full clone**。
7. 只启动临时 clone，等待 QEMU Guest Agent `ping` 和 `get-osinfo`。
8. 测试通过才把 candidate 标成 `*-current`；旧 current 改名为 `*-previous`。
9. `newvm` 永远读取 state.json 中的 current VMID。

因此构建失败或上游坏镜像不会直接破坏当前可用模板。

> 模板本身从不启动。Smoke test 启动的是临时 full clone，避免重新生成的
> machine-id / SSH host key 被固化回模板。

## 安装

在单个 Proxmox VE 节点上：

```bash
git clone https://github.com/NimaQu/proxmox-image-factory.git
cd proxmox-image-factory
chmod +x install.sh
./install.sh
```

安装脚本会安装：

- `python3-yaml`
- `libguestfs-tools`

`python3-yaml` 会在系统缺少 Python 3 时通过 APT 依赖自动安装 `python3`；如果
Proxmox VE 已经安装了它们，APT 不会重复安装。

`qemu-img` 使用 Proxmox VE 自带的 QEMU 栈；安装脚本不会安装 Debian 的
`qemu-utils`，以免与 `pve-qemu-kvm` 冲突。

然后安装到：

```text
/opt/proxmox-image-factory
/var/lib/proxmox-image-factory
/usr/local/sbin/pve-image-build
/usr/local/sbin/newvm
```

## 安装成功后第一次运行前

编辑：

```bash
nano /opt/proxmox-image-factory/config/images.yaml
```

至少检查：

```yaml
global:
  storage: local-lvm
  bridge: vmbr0
```

以及这些 VMID 没有被占用：

```text
9000 9001
9010 9011
```

如果已占用，直接改 `slots`。

## 先检查，不构建

```bash
pve-image-build --check
```

## 第一次构建全部模板

```bash
pve-image-build --all
```

只构建某几个：

```bash
pve-image-build --only debian-13 ubuntu-26.04
```

强制重建：

```bash
pve-image-build --only debian-13 --force
```

成功后：

```bash
newvm --list
```

示例：

```text
IMAGE          CURRENT  PREVIOUS  USER       CHECKSUM
debian-13      9000     -         debian     ...
ubuntu-24.04   9010     -         ubuntu     ...
ubuntu-26.04   9020     -         ubuntu     ...
rocky-9        9030     -         rocky      ...
almalinux-9    9040     -         almalinux  ...
```

下一次 Debian 镜像更新后可能变成：

```text
debian-13      9001     9000      debian     ...
```

`newvm` 会自动使用 9001。

## 创建 VM

最简单：

```bash
newvm debian-13 docker01
```

指定资源：

```bash
newvm ubuntu-26.04 web01 \
  --cores 4 \
  --memory 8192 \
  --disk 64
```

注入 SSH 公钥：

```bash
newvm debian-13 docker01 \
  --ssh-key /root/.ssh/id_ed25519.pub
```

DHCP：

```bash
newvm debian-13 vm01 --ip dhcp
```

静态 IP：

```bash
newvm debian-13 vm01 \
  --ip 192.168.10.50/24 \
  --gw 192.168.10.1 \
  --nameserver 192.168.10.1
```

指定 VMID：

```bash
newvm rocky-9 app01 --vmid 120
```

只创建不启动：

```bash
newvm almalinux-9 test01 --no-start
```

`newvm` 会在 clone 前校验 SSH key、静态 IPv4/gateway，以及 CPU、内存、磁盘
参数。输入错误时不会创建 VM。磁盘参数表示期望的最小系统盘大小；已有系统盘更大
时会保留原大小，其他 `qm resize` 错误会使命令失败并保留 VM 以便排查。

## 定时更新

默认每周日 03:00，另加 0~30 分钟随机延迟：

```bash
systemctl list-timers proxmox-image-factory.timer
```

立即手工触发：

```bash
systemctl start proxmox-image-factory.service
journalctl -u proxmox-image-factory.service -f
```

修改时间：

```bash
systemctl edit --full proxmox-image-factory.timer
```

## 关闭某个发行版

```yaml
images:
  rocky-9:
    enabled: false
```

## 增加新发行版

复制一个 image block，并提供：

```yaml
my-linux:
  enabled: true
  slots: [9050, 9051]
  template_name: my-linux
  default_user: user
  image_url: https://example/image.qcow2
  checksum_url: https://example/SHA256SUMS
  checksum_algo: sha256
  packages:
    - qemu-guest-agent
```

支持常见 checksum 格式：

```text
HASH  filename
HASH *filename
SHA256 (filename) = HASH
SHA512 (filename) = HASH
```

## 关于 cluster

这套初版假定 builder 在一个固定 PVE 节点运行，并且 template 所在 storage 对
该节点可用。

如果 `storage` 是共享存储（Ceph/RBD/NFS 等），模板磁盘可以供集群其它节点使用，
但模板 VM 配置本身仍由 PVE cluster filesystem 管理。

systemd timer 建议只在一个节点启用，避免多个节点同时构建：

```bash
systemctl disable --now proxmox-image-factory.timer
```

在你指定的 builder 节点保留启用即可。

## 安全/一致性

- 上游文件必须匹配 checksum 才会继续。
- 当前版本不会自动做 checksum 文件的 GPG signature 验证；若你的供应链要求更高，
  建议为 Debian/Ubuntu/Alma/Rocky 分别增加发行版签名密钥校验。
- `virt-customize` 和 `virt-sysprep` 只操作下载后的工作副本，不修改缓存原图。
- candidate 失败不会切换 current。
- previous slot 保留一版，方便快速 rollback。
- `newvm` 使用 full clone，避免依赖 base image；如果你想节省空间，可以自行改成 linked clone。

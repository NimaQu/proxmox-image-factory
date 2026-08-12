# Proxmox Image Factory

An automated multi-distribution cloud image template pipeline for Proxmox VE.

## Design

Each distribution is assigned two VMID slots, for example:

- Debian 13: `9000 / 9001`
- Ubuntu 26.04: `9010 / 9011`
- CentOS Stream 10 (disabled by default): `9020 / 9021`
- Arch Linux (disabled by default): `9030 / 9031`

Updates alternate between the A/B slots:

1. Check the official checksum to detect upstream changes.
2. Download and verify the official cloud image.
3. Install `qemu-guest-agent` and base packages offline with `virt-customize`.
4. Clear machine-id, SSH host keys, and persistent MAC addresses with `virt-sysprep`.
5. Import the new image into the inactive slot and convert it to a Proxmox template.
6. Create a temporary **full clone** from the candidate template.
7. Boot only the temporary clone and wait for QEMU Guest Agent `ping` and `get-osinfo`.
8. Mark the candidate as `*-current` only after the test passes, then rename the old
   current template to `*-previous`.
9. `newvm` always reads the current VMID from `state.json`.

A failed build or a broken upstream image therefore does not immediately replace the
currently usable template.

> Templates are never booted. Smoke tests boot a temporary full clone so regenerated
> machine IDs and SSH host keys are not persisted in the template.

## Installation

Run the following on a Proxmox VE node:

```bash
git clone https://github.com/NimaQu/proxmox-image-factory.git
cd proxmox-image-factory
chmod +x install.sh
./install.sh
```

The installer installs:

- `python3-yaml`
- `libguestfs-tools`

`python3-yaml` pulls in `python3` through its APT dependency if Python 3 is missing.
APT does not reinstall packages that are already present on Proxmox VE.

`qemu-img` comes from the QEMU stack provided by Proxmox VE. The installer does not
install Debian's `qemu-utils`, which may conflict with `pve-qemu-kvm`.

Files are installed under:

```text
/opt/proxmox-image-factory
/var/lib/proxmox-image-factory
/usr/local/sbin/pve-image-build
/usr/local/sbin/newvm
```

## Before the first run

Edit the installed configuration:

```bash
nano /opt/proxmox-image-factory/config/images.yaml
```

At minimum, verify:

```yaml
global:
  storage: local-lvm
  bridge: vmbr0
```

Also ensure these VMIDs are unused:

```text
9000 9001
9010 9011
9020 9021
9030 9031
```

Change the corresponding `slots` values if any VMID is already in use.

## Check without building

```bash
pve-image-build --check
```

## Build templates

Build all enabled templates for the first time:

```bash
pve-image-build --all
```

Build selected images:

```bash
pve-image-build --only debian-13 ubuntu-26.04
```

Force a rebuild:

```bash
pve-image-build --only debian-13 --force
```

List available templates after a successful build:

```bash
newvm --list
```

Example output:

```text
IMAGE             CURRENT  PREVIOUS  USER        CHECKSUM
debian-13         9000     -         debian      ...
ubuntu-26.04      9010     -         ubuntu      ...
centos-stream-10  -        -         cloud-user  -
archlinux         -        -         arch        -
```

After the next Debian image update, its row might become:

```text
debian-13         9001     9000      debian      ...
```

`newvm` will then use VMID 9001 automatically.

## Create a VM

Basic usage:

```bash
newvm debian-13 docker01
```

Specify resources:

```bash
newvm ubuntu-26.04 web01 \
  --cores 4 \
  --memory 8192 \
  --disk 64
```

Inject an SSH public key:

```bash
newvm debian-13 docker01 \
  --ssh-key /root/.ssh/id_ed25519.pub
```

Use DHCP:

```bash
newvm debian-13 vm01 --ip dhcp
```

Use a static IP:

```bash
newvm debian-13 vm01 \
  --ip 192.168.10.50/24 \
  --gw 192.168.10.1 \
  --nameserver 192.168.10.1
```

Specify a VMID:

```bash
newvm debian-13 app01 --vmid 120
```

Create without starting:

```bash
newvm archlinux test01 --no-start
```

`newvm` validates the SSH key, static IPv4/gateway, CPU, memory, and disk options
before cloning. Invalid input does not create a VM. The disk option specifies the
minimum desired system disk size. A larger existing disk is preserved; other
`qm resize` failures abort the command and leave the VM available for troubleshooting.

## Scheduled updates

The default schedule is Sunday at 03:00 with an additional randomized delay of up
to 30 minutes:

```bash
systemctl list-timers proxmox-image-factory.timer
```

Trigger a run manually:

```bash
systemctl start proxmox-image-factory.service
journalctl -u proxmox-image-factory.service -f
```

Change the schedule:

```bash
systemctl edit --full proxmox-image-factory.timer
```

## Enable or disable an image

CentOS Stream 10 and Arch Linux are disabled in the example configuration:

```yaml
images:
  centos-stream-10:
    enabled: false
  archlinux:
    enabled: false
```

Set `enabled` to `true` to enable an image, and confirm that both of its VMID slots
are unused first.

## Add a distribution

Copy an image block and provide:

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

Common checksum formats are supported:

```text
HASH  filename
HASH *filename
SHA256 (filename) = HASH
SHA512 (filename) = HASH
```

## Cluster considerations

This initial implementation assumes that the builder runs on one fixed PVE node and
that the template storage is available to that node.

Shared storage such as Ceph, RBD, or NFS makes template disks available to other
cluster nodes, while template VM configuration remains managed by the PVE cluster
filesystem.

Enable the systemd timer on only one node to avoid concurrent builds. Disable it on
all other nodes:

```bash
systemctl disable --now proxmox-image-factory.timer
```

Keep it enabled on the designated builder node.

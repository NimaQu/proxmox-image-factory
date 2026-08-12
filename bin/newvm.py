#!/usr/bin/env python3
"""
Create a Proxmox VM from the current Image Factory template.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path("/opt/proxmox-image-factory/config/images.yaml")
STATE_FILE = Path("/var/lib/proxmox-image-factory/state.json")


class NewVMError(RuntimeError):
    pass


def run(cmd: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    import shlex
    print("+ " + " ".join(shlex.quote(str(x)) for x in cmd), flush=True)
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        raise NewVMError("state.json does not exist; run pve-image-build --all first")
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def next_vmid() -> int:
    out = run(["pvesh", "get", "/cluster/nextid", "--output-format", "json"],
              capture=True).stdout.strip()
    try:
        return int(json.loads(out))
    except Exception:
        m = re.search(r"\d+", out)
        if not m:
            raise NewVMError(f"Unable to parse nextid: {out!r}")
        return int(m.group(0))


def vm_exists(vmid: int) -> bool:
    return subprocess.run(["qm", "status", str(vmid)],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def require_positive(name: str, value: int) -> int:
    if value <= 0:
        raise NewVMError(f"{name} must be a positive integer; got: {value}")
    return value


def validate_ssh_key(path: Path | None) -> None:
    if path is None:
        return
    if not path.exists():
        raise NewVMError(f"SSH key does not exist: {path}")
    if not path.is_file():
        raise NewVMError(f"SSH key is not a regular file: {path}")
    try:
        if not path.read_text(encoding="utf-8").strip():
            raise NewVMError(f"SSH key file is empty: {path}")
    except (OSError, UnicodeError) as e:
        raise NewVMError(f"Unable to read SSH key {path}: {e}") from e


def make_ipconfig(ip: str, gateway: str | None) -> str:
    if ip == "dhcp":
        if gateway:
            raise NewVMError("--gw cannot be used with DHCP")
        return "ip=dhcp"

    try:
        interface = ipaddress.ip_interface(ip)
    except ValueError as e:
        raise NewVMError(f"Invalid static IP/CIDR: {ip}") from e
    if interface.version != 4:
        raise NewVMError("--ip/--gw currently support IPv4 only; IPv6 requires separate ip6/gw6 options")
    if not gateway:
        raise NewVMError("A static IP requires --gw")
    try:
        gateway_ip = ipaddress.ip_address(gateway)
    except ValueError as e:
        raise NewVMError(f"Invalid gateway: {gateway}") from e
    if gateway_ip.version != 4:
        raise NewVMError("--gw must be an IPv4 address")
    return f"ip={interface},gw={gateway_ip}"


def wait_agent(vmid: int, timeout: int = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if subprocess.run(["qm", "agent", str(vmid), "ping"],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            return True
        time.sleep(3)
    return False


def get_ipv4(vmid: int) -> list[str]:
    cp = subprocess.run(
        ["qm", "agent", str(vmid), "network-get-interfaces"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    if cp.returncode != 0:
        return []
    try:
        data = json.loads(cp.stdout)
    except Exception:
        return []

    # Depending on the qm version, the response may be a list or wrapped in result.
    if isinstance(data, dict) and "result" in data:
        data = data["result"]

    ips = []
    if not isinstance(data, list):
        return ips
    for iface in data:
        for item in iface.get("ip-addresses", []) or []:
            if item.get("ip-address-type") != "ipv4":
                continue
            ip = item.get("ip-address")
            if not ip or ip.startswith("127."):
                continue
            try:
                obj = ipaddress.ip_address(ip)
                if not obj.is_link_local:
                    ips.append(ip)
            except ValueError:
                pass
    return sorted(set(ips))


def list_images(cfg: dict[str, Any], state: dict[str, Any]) -> None:
    rows = []
    for name, icfg in cfg["images"].items():
        st = state.get("images", {}).get(name, {})
        rows.append((
            name,
            st.get("current_vmid", "-"),
            st.get("previous_vmid", "-"),
            icfg.get("default_user", "-"),
            (st.get("checksum") or "-")[:12],
        ))
    widths = [max(len(str(r[i])) for r in rows + [("IMAGE","CURRENT","PREVIOUS","USER","CHECKSUM")])
              for i in range(5)]
    header = ("IMAGE","CURRENT","PREVIOUS","USER","CHECKSUM")
    print("  ".join(str(header[i]).ljust(widths[i]) for i in range(5)))
    print("  ".join("-" * widths[i] for i in range(5)))
    for r in rows:
        print("  ".join(str(r[i]).ljust(widths[i]) for i in range(5)))


def main() -> int:
    ap = argparse.ArgumentParser(description="Clone VM from current Proxmox image-factory template")
    ap.add_argument("image", nargs="?", help="Image name, for example debian-13 or ubuntu-26.04")
    ap.add_argument("name", nargs="?", help="Name of the new VM")
    ap.add_argument("--list", action="store_true", help="List available images and current VMIDs")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--vmid", type=int)
    ap.add_argument("--cores", type=int)
    ap.add_argument("--memory", type=int, help="MB")
    ap.add_argument("--disk", type=int, help="Final system disk size in GB (grow only)")
    ap.add_argument("--storage", help="Clone target storage; defaults to global.storage")
    ap.add_argument("--bridge", help="Network bridge; defaults to global.bridge")
    ap.add_argument("--user", help="Cloud-Init username")
    ap.add_argument("--ssh-key", type=Path, help="SSH public key file")
    ap.add_argument("--ip", default="dhcp", help="dhcp or a CIDR such as 192.168.1.50/24")
    ap.add_argument("--gw", help="Gateway for a static IP")
    ap.add_argument("--nameserver", help="For example 1.1.1.1")
    ap.add_argument("--searchdomain")
    ap.add_argument("--no-start", action="store_true")
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        raise NewVMError("Must be run as root")
    if shutil.which("qm") is None:
        raise NewVMError("qm was not found; run this command on a Proxmox VE node")

    cfg = load_yaml(args.config)
    state = load_state()

    if args.list:
        list_images(cfg, state)
        return 0

    if not args.image or not args.name:
        ap.error("IMAGE and NAME are required, or use --list")
    if args.image not in cfg["images"]:
        raise NewVMError(f"Unknown image {args.image!r}; use newvm --list")

    icfg = cfg["images"][args.image]
    st = state.get("images", {}).get(args.image, {})
    src = st.get("current_vmid")
    if not src or not vm_exists(int(src)):
        raise NewVMError(f"{args.image} has no usable current template")

    g = cfg.get("global", {})
    vmid = args.vmid or next_vmid()
    if vm_exists(vmid):
        raise NewVMError(f"VMID {vmid} already exists")

    storage = args.storage or icfg.get("storage") or g["storage"]
    bridge = args.bridge or icfg.get("bridge") or g["bridge"]
    cores = args.cores if args.cores is not None else int(g.get("default_cores", 2))
    memory = args.memory if args.memory is not None else int(g.get("default_memory_mb", 2048))
    disk = args.disk if args.disk is not None else int(g.get("default_disk_gb", 32))
    user = args.user or icfg.get("default_user")

    # Validate every input that does not depend on the new VM before cloning.
    if not str(storage).strip():
        raise NewVMError("storage cannot be empty")
    if not str(bridge).strip():
        raise NewVMError("bridge cannot be empty")
    cores = require_positive("cores", cores)
    memory = require_positive("memory", memory)
    disk = require_positive("disk", disk)
    validate_ssh_key(args.ssh_key)
    ipconfig = make_ipconfig(args.ip, args.gw)

    created = False
    try:
        run([
            "qm", "clone", str(src), str(vmid),
            "--name", args.name,
            "--full", "1",
            "--storage", str(storage),
        ])
        created = True

        run(["qm", "set", str(vmid), "--cores", str(cores), "--memory", str(memory)])
        # Rewrite net0 to generate a new random MAC and allow a different bridge.
        run(["qm", "set", str(vmid), "--net0", f"virtio,bridge={bridge}"])

        if user:
            run(["qm", "set", str(vmid), "--ciuser", str(user)])

        if args.ssh_key:
            run(["qm", "set", str(vmid), "--sshkeys", str(args.ssh_key)])

        run(["qm", "set", str(vmid), "--ipconfig0", ipconfig])

        if args.nameserver:
            run(["qm", "set", str(vmid), "--nameserver", args.nameserver])
        if args.searchdomain:
            run(["qm", "set", str(vmid), "--searchdomain", args.searchdomain])

        # Cloud image root disks are usually smaller than the default; qm rejects shrinking.
        # If the requested size is smaller, keep the original size and print a warning.
        cp = subprocess.run(
            ["qm", "resize", str(vmid), "scsi0", f"{disk}G"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        if cp.returncode != 0:
            output = cp.stdout.strip()
            # A rejected shrink is the only resize failure that is safe to ignore.
            if re.search(r"(?:shrink|shrinking|smaller|cannot reduce|unable to reduce)",
                         output, re.IGNORECASE):
                print(f"WARN: System disk is already at least {disk}G; keeping its current size: {output}", file=sys.stderr)
            else:
                raise NewVMError(f"Failed to resize the system disk to {disk}G: {output or 'qm resize returned no details'}")

        if args.no_start:
            print(f"\nCreated VM {vmid} ({args.name}), not started.")
            return 0

        run(["qm", "start", str(vmid)])
        print(f"\nVM {vmid} started.")

        if not args.no_wait:
            if wait_agent(vmid, 180):
                ips = get_ipv4(vmid)
                print("QEMU Guest Agent: OK")
                if ips:
                    print("IPv4: " + ", ".join(ips))
                else:
                    print("IPv4: the agent responded, but no address was found yet")
            else:
                print("WARN: QEMU Guest Agent did not respond within 180 seconds; the VM remains running.", file=sys.stderr)

        print(f"Template: {args.image} (VMID {src})")
        print(f"VMID: {vmid}")
        print(f"Name: {args.name}")
        return 0

    except Exception:
        if created:
            print(f"ERROR: Failed while creating VM {vmid}; the VM was kept for troubleshooting.", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (NewVMError, subprocess.CalledProcessError, ValueError) as e:
        print(f"FATAL: {e}", file=sys.stderr)
        raise SystemExit(1)

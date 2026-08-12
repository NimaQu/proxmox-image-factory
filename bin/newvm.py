#!/usr/bin/env python3
"""
从 Image Factory 当前模板快速创建 Proxmox VM。
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
        raise NewVMError("state.json 不存在；请先运行 pve-image-build --all")
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def next_vmid() -> int:
    out = run(["pvesh", "get", "/cluster/nextid", "--output-format", "json"],
              capture=True).stdout.strip()
    try:
        return int(json.loads(out))
    except Exception:
        m = re.search(r"\d+", out)
        if not m:
            raise NewVMError(f"无法解析 nextid: {out!r}")
        return int(m.group(0))


def vm_exists(vmid: int) -> bool:
    return subprocess.run(["qm", "status", str(vmid)],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def require_positive(name: str, value: int) -> int:
    if value <= 0:
        raise NewVMError(f"{name} 必须是正整数，当前值: {value}")
    return value


def validate_ssh_key(path: Path | None) -> None:
    if path is None:
        return
    if not path.exists():
        raise NewVMError(f"SSH key 不存在: {path}")
    if not path.is_file():
        raise NewVMError(f"SSH key 不是普通文件: {path}")
    try:
        if not path.read_text(encoding="utf-8").strip():
            raise NewVMError(f"SSH key 文件为空: {path}")
    except (OSError, UnicodeError) as e:
        raise NewVMError(f"无法读取 SSH key: {path}: {e}") from e


def make_ipconfig(ip: str, gateway: str | None) -> str:
    if ip == "dhcp":
        if gateway:
            raise NewVMError("DHCP 模式不能同时指定 --gw")
        return "ip=dhcp"

    try:
        interface = ipaddress.ip_interface(ip)
    except ValueError as e:
        raise NewVMError(f"无效的静态 IP/CIDR: {ip}") from e
    if interface.version != 4:
        raise NewVMError("--ip/--gw 当前只支持 IPv4；IPv6 需要单独的 ip6/gw6 参数")
    if not gateway:
        raise NewVMError("静态 IP 必须同时给 --gw")
    try:
        gateway_ip = ipaddress.ip_address(gateway)
    except ValueError as e:
        raise NewVMError(f"无效的 gateway: {gateway}") from e
    if gateway_ip.version != 4:
        raise NewVMError("--gw 必须是 IPv4 地址")
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

    # qm 版本不同，结果可能直接为 list，也可能套 result。
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
    ap.add_argument("image", nargs="?", help="镜像名，如 debian-13 / ubuntu-26.04")
    ap.add_argument("name", nargs="?", help="新 VM 名称")
    ap.add_argument("--list", action="store_true", help="列出可用镜像/current VMID")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--vmid", type=int)
    ap.add_argument("--cores", type=int)
    ap.add_argument("--memory", type=int, help="MB")
    ap.add_argument("--disk", type=int, help="系统盘最终大小，GB（只能扩容）")
    ap.add_argument("--storage", help="clone 目标 storage，默认 global.storage")
    ap.add_argument("--bridge", help="网桥，默认 global.bridge")
    ap.add_argument("--user", help="Cloud-Init 用户名")
    ap.add_argument("--ssh-key", type=Path, help="SSH 公钥文件")
    ap.add_argument("--ip", default="dhcp", help="dhcp 或 CIDR，例如 192.168.1.50/24")
    ap.add_argument("--gw", help="静态 IP 时的 gateway")
    ap.add_argument("--nameserver", help="例如 1.1.1.1")
    ap.add_argument("--searchdomain")
    ap.add_argument("--no-start", action="store_true")
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        raise NewVMError("必须以 root 运行")
    if shutil.which("qm") is None:
        raise NewVMError("找不到 qm；请在 Proxmox VE 节点上运行")

    cfg = load_yaml(args.config)
    state = load_state()

    if args.list:
        list_images(cfg, state)
        return 0

    if not args.image or not args.name:
        ap.error("需要 IMAGE 和 NAME；或者使用 --list")
    if args.image not in cfg["images"]:
        raise NewVMError(f"未知镜像 {args.image!r}；用 newvm --list 查看")

    icfg = cfg["images"][args.image]
    st = state.get("images", {}).get(args.image, {})
    src = st.get("current_vmid")
    if not src or not vm_exists(int(src)):
        raise NewVMError(f"{args.image} 没有可用 current template")

    g = cfg.get("global", {})
    vmid = args.vmid or next_vmid()
    if vm_exists(vmid):
        raise NewVMError(f"VMID {vmid} 已存在")

    storage = args.storage or icfg.get("storage") or g["storage"]
    bridge = args.bridge or icfg.get("bridge") or g["bridge"]
    cores = args.cores if args.cores is not None else int(g.get("default_cores", 2))
    memory = args.memory if args.memory is not None else int(g.get("default_memory_mb", 2048))
    disk = args.disk if args.disk is not None else int(g.get("default_disk_gb", 32))
    user = args.user or icfg.get("default_user")

    # 所有不依赖新 VM 的输入都必须在 clone 前校验，避免留下半成品 VM。
    if not str(storage).strip():
        raise NewVMError("storage 不能为空")
    if not str(bridge).strip():
        raise NewVMError("bridge 不能为空")
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
        # 重新写 net0：生成新的随机 MAC，并允许选择新 bridge。
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

        # cloud image 根盘通常远小于这里的默认值；qm 会拒绝缩容。
        # 若用户指定的值比当前盘小，保留原大小并打印警告。
        cp = subprocess.run(
            ["qm", "resize", str(vmid), "scsi0", f"{disk}G"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        if cp.returncode != 0:
            output = cp.stdout.strip()
            # 目标不大于当前磁盘时，PVE 会拒绝缩容；这是唯一可安全忽略的情况。
            if re.search(r"(?:shrink|shrinking|smaller|cannot reduce|unable to reduce)",
                         output, re.IGNORECASE):
                print(f"WARN: 系统盘已不小于 {disk}G，保持原大小：{output}", file=sys.stderr)
            else:
                raise NewVMError(f"调整系统盘到 {disk}G 失败: {output or 'qm resize 未返回错误详情'}")

        if args.no_start:
            print(f"\nCreated VM {vmid} ({args.name}), not started.")
            return 0

        run(["qm", "start", str(vmid)])
        print(f"\nVM {vmid} 已启动。")

        if not args.no_wait:
            if wait_agent(vmid, 180):
                ips = get_ipv4(vmid)
                print("QEMU Guest Agent: OK")
                if ips:
                    print("IPv4: " + ", ".join(ips))
                else:
                    print("IPv4: agent 已响应，但暂未解析到地址")
            else:
                print("WARN: 180 秒内 QEMU Guest Agent 未响应；VM 仍保持运行。", file=sys.stderr)

        print(f"Template: {args.image} (VMID {src})")
        print(f"VMID: {vmid}")
        print(f"Name: {args.name}")
        return 0

    except Exception:
        if created:
            print(f"ERROR: 创建 VM {vmid} 过程中失败；VM 保留以便排查。", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (NewVMError, subprocess.CalledProcessError, ValueError) as e:
        print(f"FATAL: {e}", file=sys.stderr)
        raise SystemExit(1)

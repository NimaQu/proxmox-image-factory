#!/usr/bin/env python3
"""
Proxmox Image Factory

- 从发行版官方 cloud image + checksum 构建 Proxmox 模板
- 使用 virt-customize 离线安装 qemu-guest-agent 等基础包
- 使用 virt-sysprep 清理 machine-id / SSH host keys / 固化 MAC
- A/B 两槽滚动：current 保留到新模板 smoke test 通过
- smoke test 在模板的临时 full clone 上进行，因此模板本身从不启动
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path("/opt/proxmox-image-factory/config/images.yaml")
STATE_DIR = Path("/var/lib/proxmox-image-factory")
STATE_FILE = STATE_DIR / "state.json"
CACHE_DIR = STATE_DIR / "cache"
WORK_DIR = STATE_DIR / "work"
LOCK_FILE = STATE_DIR / "factory.lock"

SYSPREP_OPS = ["machine-id", "ssh-hostkeys", "net-hwaddr"]


class FactoryError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def run(cmd: list[str], *, check: bool = True, capture: bool = False,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    log("+ " + " ".join(shlex_quote(x) for x in cmd))
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        env=merged_env,
    )


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(str(s))


def require_root() -> None:
    if os.geteuid() != 0:
        raise FactoryError("必须以 root 运行。")


def require_commands() -> None:
    needed = ["qm", "pvesh", "qemu-img", "virt-customize", "virt-sysprep"]
    missing = [x for x in needed if shutil.which(x) is None]
    if missing:
        raise FactoryError("缺少命令: " + ", ".join(missing))


def ensure_dirs() -> None:
    for p in (STATE_DIR, CACHE_DIR, WORK_DIR):
        p.mkdir(parents=True, exist_ok=True)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "images" not in data:
        raise FactoryError(f"无效配置文件: {path}")
    return data


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"images": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise FactoryError(f"读取 state.json 失败: {e}") from e


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def http_get_text(url: str, timeout: int = 45) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "proxmox-image-factory/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def download(url: str, dest: Path, expected_hash: str, algo: str) -> None:
    partial = dest.with_suffix(dest.suffix + ".partial")
    h = hashlib.new(algo)
    req = urllib.request.Request(url, headers={"User-Agent": "proxmox-image-factory/1.0"})
    log(f"下载: {url}")
    with urllib.request.urlopen(req, timeout=60) as r, partial.open("wb") as f:
        while True:
            chunk = r.read(4 * 1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)

    got = h.hexdigest().lower()
    if got != expected_hash.lower():
        partial.unlink(missing_ok=True)
        raise FactoryError(f"checksum 不匹配: expected={expected_hash}, got={got}")

    os.replace(partial, dest)
    log(f"校验通过: {algo}:{got}")


def hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_checksum(text: str, filename: str, algo: str) -> str:
    """
    支持常见格式：
      HASH  filename
      HASH *filename
      SHA256 (filename) = HASH
      SHA512 (filename) = HASH
    """
    base = Path(filename).name
    want_len = hashlib.new(algo).digest_size * 2

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        m = re.match(r"^([0-9A-Fa-f]+)\s+\*?(.+?)\s*$", line)
        if m:
            h, fn = m.group(1), m.group(2).strip()
            if Path(fn).name == base and len(h) == want_len:
                return h.lower()

        m = re.match(r"^(SHA256|SHA512)\s+\((.+?)\)\s*=\s*([0-9A-Fa-f]+)\s*$", line, re.I)
        if m:
            alg_name, fn, h = m.groups()
            normalized = alg_name.lower()
            if normalized == algo.lower() and Path(fn).name == base and len(h) == want_len:
                return h.lower()

        # Rocky 的 CHECKSUM 有时可能包含 "SHA256 (file) = hash" 前后的说明，
        # 这里再做一个宽松匹配，但仍要求文件名和 hash 长度精确。
        if base in line:
            candidates = re.findall(rf"\b[0-9A-Fa-f]{{{want_len}}}\b", line)
            if candidates:
                return candidates[0].lower()

    raise FactoryError(f"在 checksum 文件中找不到 {base} 的 {algo}")


def vm_exists(vmid: int) -> bool:
    cp = subprocess.run(["qm", "status", str(vmid)], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
    return cp.returncode == 0


def qm_config(vmid: int) -> str:
    return run(["qm", "config", str(vmid)], capture=True).stdout or ""


def destroy_vm(vmid: int) -> None:
    if not vm_exists(vmid):
        return
    subprocess.run(["qm", "stop", str(vmid), "--skiplock", "1"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run(["qm", "destroy", str(vmid), "--purge", "1", "--destroy-unreferenced-disks", "1"])


def next_vmid() -> int:
    out = run(["pvesh", "get", "/cluster/nextid", "--output-format", "json"],
              capture=True).stdout.strip()
    try:
        return int(json.loads(out))
    except Exception:
        m = re.search(r"\d+", out)
        if not m:
            raise FactoryError(f"无法解析 nextid: {out!r}")
        return int(m.group(0))


def imported_unused_disk(vmid: int) -> str:
    cfg = qm_config(vmid)
    candidates = []
    for line in cfg.splitlines():
        if line.startswith("unused"):
            _, value = line.split(":", 1)
            volid = value.strip().split(",", 1)[0]
            candidates.append(volid)
    if len(candidates) != 1:
        raise FactoryError(f"VM {vmid} 导入后预期 1 个 unused disk，实际 {len(candidates)} 个")
    return candidates[0]


def guestfs_env() -> dict[str, str]:
    return {
        "LIBGUESTFS_BACKEND": os.environ.get("LIBGUESTFS_BACKEND", "direct"),
        "LIBGUESTFS_MEMSIZE": os.environ.get("LIBGUESTFS_MEMSIZE", "1024"),
    }


def customize_image(src: Path, dest: Path, image_cfg: dict[str, Any],
                    global_cfg: dict[str, Any]) -> None:
    # 保留缓存的官方原图；每次从原图转换出独立 qcow2 工作副本。
    dest.unlink(missing_ok=True)
    run(["qemu-img", "convert", "-p", "-O", "qcow2", str(src), str(dest)])

    packages = list(image_cfg.get("packages", []))
    cmd = ["virt-customize", "-a", str(dest), "--network"]
    if global_cfg.get("update_packages", False):
        cmd.append("--update")
    if packages:
        cmd += ["--install", ",".join(packages)]

    # qemu-guest-agent 包安装后，明确 enable；某些镜像中 unit 是 static，
    # 因此失败不作为构建失败。
    cmd += [
        "--run-command",
        "systemctl enable qemu-guest-agent.service 2>/dev/null || true",
        "--run-command",
        "systemctl enable qemu-guest-agent 2>/dev/null || true",
    ]
    run(cmd, env=guestfs_env())

    # 只做和克隆唯一性直接相关的清理，避免过度 sysprep。
    run([
        "virt-sysprep", "-a", str(dest),
        "--operations", ",".join(SYSPREP_OPS),
    ], env=guestfs_env())


def create_template_from_disk(vmid: int, name: str, disk: Path,
                              storage: str, bridge: str,
                              global_cfg: dict[str, Any]) -> None:
    if vm_exists(vmid):
        destroy_vm(vmid)

    run([
        "qm", "create", str(vmid),
        "--name", name,
        "--ostype", "l26",
        "--memory", str(global_cfg.get("template_memory_mb", 1024)),
        "--cores", str(global_cfg.get("template_cores", 1)),
        "--scsihw", "virtio-scsi-pci",
        "--net0", f"virtio,bridge={bridge}",
        "--serial0", "socket",
        "--vga", "serial0",
        "--agent", str(global_cfg.get("agent", "1")),
    ])

    try:
        run(["qm", "importdisk", str(vmid), str(disk), storage])
        volid = imported_unused_disk(vmid)
        run(["qm", "set", str(vmid), "--scsi0", f"{volid},discard=on,ssd=1"])
        run(["qm", "set", str(vmid), "--ide2", f"{storage}:cloudinit"])
        run(["qm", "set", str(vmid), "--boot", "order=scsi0"])
        run(["qm", "set", str(vmid), "--ipconfig0", "ip=dhcp"])
        run(["qm", "template", str(vmid)])
    except Exception:
        destroy_vm(vmid)
        raise


def wait_agent(vmid: int, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cp = subprocess.run(["qm", "agent", str(vmid), "ping"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if cp.returncode == 0:
            return True
        time.sleep(5)
    return False


def smoke_test(template_vmid: int, storage: str, timeout: int,
               shutdown_timeout: int) -> None:
    test_vmid = next_vmid()
    test_name = f"imgfactory-smoke-{template_vmid}-{test_vmid}"

    try:
        run([
            "qm", "clone", str(template_vmid), str(test_vmid),
            "--name", test_name,
            "--full", "1",
            "--storage", storage,
        ])
        run(["qm", "start", str(test_vmid)])

        if not wait_agent(test_vmid, timeout):
            raise FactoryError(
                f"smoke test 失败：VM {test_vmid} 在 {timeout}s 内 QEMU Guest Agent 未响应"
            )

        # 能拿到 osinfo，进一步证明 guest-agent 通道工作。
        run(["qm", "agent", str(test_vmid), "get-osinfo"], capture=True)

        # 正常关机优先；失败再 destroy 时强制 stop。
        cp = subprocess.run(
            ["qm", "shutdown", str(test_vmid), "--timeout", str(shutdown_timeout)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if cp.returncode != 0:
            subprocess.run(["qm", "stop", str(test_vmid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        destroy_vm(test_vmid)


def set_vm_name(vmid: int, name: str) -> None:
    if vm_exists(vmid):
        run(["qm", "set", str(vmid), "--name", name])


def choose_inactive_slot(name: str, cfg: dict[str, Any],
                         state: dict[str, Any]) -> tuple[int, int | None]:
    slots = cfg.get("slots")
    if not isinstance(slots, list) or len(slots) != 2:
        raise FactoryError(f"{name}: slots 必须恰好包含两个 VMID")
    a, b = int(slots[0]), int(slots[1])

    current = state.get("images", {}).get(name, {}).get("current_vmid")
    if current in (a, b) and vm_exists(int(current)):
        inactive = b if int(current) == a else a
        return inactive, int(current)

    # state 丢失时，尽量根据现有 VM 判断；若两个都存在，优先保留 slot A。
    exists_a, exists_b = vm_exists(a), vm_exists(b)
    if exists_a and not exists_b:
        return b, a
    if exists_b and not exists_a:
        return a, b
    if exists_a and exists_b:
        return b, a
    return a, None


def build_one(name: str, image_cfg: dict[str, Any], global_cfg: dict[str, Any],
              state: dict[str, Any], *, force: bool = False,
              check_only: bool = False) -> bool:
    image_url = str(image_cfg["image_url"])
    checksum_url = str(image_cfg["checksum_url"])
    algo = str(image_cfg.get("checksum_algo", "sha256")).lower()
    filename = Path(urllib.parse.urlparse(image_url).path).name

    log(f"===== {name} =====")
    checksum_text = http_get_text(checksum_url)
    upstream_hash = parse_checksum(checksum_text, filename, algo)
    old = state.get("images", {}).get(name, {})
    old_hash = old.get("checksum")
    current_vmid = old.get("current_vmid")

    changed = upstream_hash != old_hash or not current_vmid or not vm_exists(int(current_vmid))
    if check_only:
        status = "UPDATE" if changed else "OK"
        log(f"{name}: {status} upstream={upstream_hash[:16]} current={str(old_hash)[:16]}")
        return changed

    if not force and not changed:
        log(f"{name}: upstream checksum 未变化，跳过")
        return False

    cache_name = f"{name}-{upstream_hash[:16]}-{filename}"
    cached = CACHE_DIR / cache_name
    if cached.exists():
        got = hash_file(cached, algo)
        if got != upstream_hash:
            log("缓存 checksum 不匹配，重新下载")
            cached.unlink()
    if not cached.exists():
        download(image_url, cached, upstream_hash, algo)
    else:
        log(f"使用已校验缓存: {cached}")

    inactive, current = choose_inactive_slot(name, image_cfg, state)
    log(f"{name}: current={current}, build_slot={inactive}")

    # 只销毁 inactive/previous slot；current 在新版本通过测试前绝不动。
    if vm_exists(inactive):
        log(f"{name}: 清理旧 previous slot {inactive}")
        destroy_vm(inactive)

    work = WORK_DIR / f"{name}-{inactive}.qcow2"
    storage = str(image_cfg.get("storage", global_cfg["storage"]))
    bridge = str(image_cfg.get("bridge", global_cfg["bridge"]))
    base_name = str(image_cfg.get("template_name", name))

    try:
        customize_image(cached, work, image_cfg, global_cfg)
        create_template_from_disk(
            inactive,
            f"{base_name}-candidate",
            work,
            storage,
            bridge,
            global_cfg,
        )

        smoke_test(
            inactive,
            storage,
            int(global_cfg.get("smoke_timeout_sec", 240)),
            int(global_cfg.get("shutdown_timeout_sec", 60)),
        )

        # 测试通过后才切换 current 标识。
        if current is not None and vm_exists(current):
            set_vm_name(current, f"{base_name}-previous")
        set_vm_name(inactive, f"{base_name}-current")

        state.setdefault("images", {})[name] = {
            "current_vmid": inactive,
            "previous_vmid": current,
            "checksum": upstream_hash,
            "checksum_algo": algo,
            "source_url": image_url,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        save_state(state)
        log(f"{name}: SUCCESS current VMID={inactive}, previous={current}")
        return True

    except Exception:
        log(f"{name}: FAILED；保留原 current={current}")
        # candidate 可以安全删除；旧 current 从未启动/修改。
        if vm_exists(inactive):
            destroy_vm(inactive)
        raise
    finally:
        work.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build/update Proxmox cloud-image templates")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--all", action="store_true", help="构建所有 enabled 镜像")
    ap.add_argument("--only", nargs="+", help="只处理指定镜像，如 --only debian-13 ubuntu-26.04")
    ap.add_argument("--force", action="store_true", help="即使 checksum 未变化也强制重建")
    ap.add_argument("--check", action="store_true", help="只检查上游是否有更新，不下载/构建")
    args = ap.parse_args()

    require_root()
    require_commands()
    ensure_dirs()

    cfg = load_yaml(args.config)
    global_cfg = cfg.get("global", {})
    images = cfg["images"]

    if args.only:
        selected = args.only
    elif args.all or args.check:
        selected = [name for name, icfg in images.items() if icfg.get("enabled", True)]
    else:
        ap.error("请使用 --all、--only ... 或 --check")

    unknown = [x for x in selected if x not in images]
    if unknown:
        raise FactoryError("未知镜像: " + ", ".join(unknown))

    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FactoryError("另一个 image-factory 任务正在运行")

        state = load_state()
        failures = []
        changed_count = 0

        for name in selected:
            icfg = images[name]
            if not icfg.get("enabled", True) and not args.only:
                continue
            try:
                changed = build_one(
                    name, icfg, global_cfg, state,
                    force=args.force,
                    check_only=args.check,
                )
                changed_count += int(bool(changed))
            except Exception as e:
                failures.append((name, str(e)))
                log(f"ERROR {name}: {e}")

        if failures:
            log("失败汇总:")
            for name, err in failures:
                log(f"  {name}: {err}")
            return 2

        if args.check:
            log(f"检查完成：{changed_count} 个镜像有更新")
        else:
            log(f"完成：{changed_count} 个模板发生更新")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FactoryError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
"""
Proxmox Image Factory

- Build Proxmox templates from official cloud images and checksums
- Install qemu-guest-agent and base packages offline with virt-customize
- Clear machine-id, SSH host keys, and persistent MAC addresses with virt-sysprep
- Use A/B slots and retain current until the candidate passes its smoke test
- Run smoke tests on temporary full clones; templates are never booted
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
        raise FactoryError("Must be run as root.")


def require_commands() -> None:
    needed = ["qm", "pvesh", "qemu-img", "virt-customize", "virt-sysprep"]
    missing = [x for x in needed if shutil.which(x) is None]
    if missing:
        raise FactoryError("Missing commands: " + ", ".join(missing))


def ensure_dirs() -> None:
    for p in (STATE_DIR, CACHE_DIR, WORK_DIR):
        p.mkdir(parents=True, exist_ok=True)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "images" not in data:
        raise FactoryError(f"Invalid configuration file: {path}")
    return data


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"images": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise FactoryError(f"Failed to read state.json: {e}") from e


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
    log(f"Downloading: {url}")
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
        raise FactoryError(f"Checksum mismatch: expected={expected_hash}, got={got}")

    os.replace(partial, dest)
    log(f"Checksum verified: {algo}:{got}")


def hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_checksum(text: str, filename: str, algo: str) -> str:
    """
    Supported formats:
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

        # Rocky CHECKSUM files may include explanatory text around
        # "SHA256 (file) = hash". Match loosely but require an exact filename and hash length.
        if base in line:
            candidates = re.findall(rf"\b[0-9A-Fa-f]{{{want_len}}}\b", line)
            if candidates:
                return candidates[0].lower()

    raise FactoryError(f"No {algo} checksum for {base} was found in the checksum file")


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
            raise FactoryError(f"Unable to parse nextid: {out!r}")
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
        raise FactoryError(f"Expected one unused disk after importing VM {vmid}; found {len(candidates)}")
    return candidates[0]


def guestfs_env() -> dict[str, str]:
    return {
        "LIBGUESTFS_BACKEND": os.environ.get("LIBGUESTFS_BACKEND", "direct"),
        "LIBGUESTFS_MEMSIZE": os.environ.get("LIBGUESTFS_MEMSIZE", "1024"),
    }


def customize_image(src: Path, dest: Path, image_cfg: dict[str, Any],
                    global_cfg: dict[str, Any]) -> None:
    # Preserve the cached official image and create a separate qcow2 working copy.
    dest.unlink(missing_ok=True)
    run(["qemu-img", "convert", "-p", "-O", "qcow2", str(src), str(dest)])

    packages = list(image_cfg.get("packages", []))
    cmd = ["virt-customize", "-a", str(dest), "--network"]
    if global_cfg.get("update_packages", False):
        cmd.append("--update")
    if packages:
        cmd += ["--install", ",".join(packages)]

    # Explicitly enable qemu-guest-agent after installation. The unit is static in
    # some images, so failure here does not fail the build.
    cmd += [
        "--run-command",
        "systemctl enable qemu-guest-agent.service 2>/dev/null || true",
        "--run-command",
        "systemctl enable qemu-guest-agent 2>/dev/null || true",
    ]
    run(cmd, env=guestfs_env())

    # Limit sysprep to operations required for clone uniqueness.
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
                f"Smoke test failed: QEMU Guest Agent on VM {test_vmid} did not respond within {timeout}s"
            )

        # Fetching osinfo further verifies that the guest-agent channel works.
        run(["qm", "agent", str(test_vmid), "get-osinfo"], capture=True)

        # Prefer a clean shutdown; force-stop before destruction if it fails.
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
        raise FactoryError(f"{name}: slots must contain exactly two VMIDs")
    a, b = int(slots[0]), int(slots[1])

    current = state.get("images", {}).get(name, {}).get("current_vmid")
    if current in (a, b) and vm_exists(int(current)):
        inactive = b if int(current) == a else a
        return inactive, int(current)

    # If state is missing, infer it from existing VMs. Preserve slot A if both exist.
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
        log(f"{name}: upstream checksum is unchanged; skipping")
        return False

    cache_name = f"{name}-{upstream_hash[:16]}-{filename}"
    cached = CACHE_DIR / cache_name
    if cached.exists():
        got = hash_file(cached, algo)
        if got != upstream_hash:
            log("Cached checksum does not match; downloading again")
            cached.unlink()
    if not cached.exists():
        download(image_url, cached, upstream_hash, algo)
    else:
        log(f"Using verified cache: {cached}")

    inactive, current = choose_inactive_slot(name, image_cfg, state)
    log(f"{name}: current={current}, build_slot={inactive}")

    # Destroy only the inactive/previous slot; never touch current before validation.
    if vm_exists(inactive):
        log(f"{name}: removing old previous slot {inactive}")
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

        # Switch the current marker only after the test passes.
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
        log(f"{name}: FAILED; preserving original current={current}")
        # The candidate is safe to delete; the old current was never booted or modified.
        if vm_exists(inactive):
            destroy_vm(inactive)
        raise
    finally:
        work.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build/update Proxmox cloud-image templates")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--all", action="store_true", help="Build all enabled images")
    ap.add_argument("--only", nargs="+", help="Process selected images, for example --only debian-13 ubuntu-26.04")
    ap.add_argument("--force", action="store_true", help="Rebuild even when the checksum is unchanged")
    ap.add_argument("--check", action="store_true", help="Check for upstream updates without downloading or building")
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
        ap.error("Use --all, --only ..., or --check")

    unknown = [x for x in selected if x not in images]
    if unknown:
        raise FactoryError("Unknown images: " + ", ".join(unknown))

    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FactoryError("Another image-factory task is already running")

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
            log("Failure summary:")
            for name, err in failures:
                log(f"  {name}: {err}")
            return 2

        if args.check:
            log(f"Check complete: {changed_count} image(s) have updates")
        else:
            log(f"Complete: {changed_count} template(s) updated")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FactoryError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        raise SystemExit(1)

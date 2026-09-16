"""Local check: can this host thaw a Firecracker snapshot.

The state you want is RestoreState: freeze RAM + disk, then resume on a new
sandbox_id. ReplaySpan is a different thing: it runs the old exec commands
again. This check only proves RestoreState.

    python3 -m ew_examples.vm_replay --diagnose
    python3 -m ew_examples.vm_replay --assets /tmp/trace-fc-assets

Exit 0 = PASS, 2 = SKIP (no kvm or assets), 1 = FAIL.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

from .fc import FirecrackerConfig
from .store import TraceError
from .trace import Runtime

PKG = os.path.dirname(os.path.abspath(__file__))
AGENT_C = os.path.join(PKG, "guest_agent.c")
DEFAULT_ASSETS = os.environ.get("TRACE_FC_ASSETS", "/tmp/trace-fc-assets")

FC_TGZ_URL = (
    "https://github.com/firecracker-microvm/firecracker/releases/"
    "download/v1.16.1/firecracker-v1.16.1-x86_64.tgz"
)
KERNEL_URL = (
    "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/"
    "v1.13/x86_64/vmlinux-6.1.186"
)
ALPINE_URL = (
    "https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/x86_64/"
    "alpine-minirootfs-3.20.3-x86_64.tar.gz"
)

MARKER = "keepme"


def have_kvm() -> bool:
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def asset_paths(assets: str) -> dict[str, str]:
    root = os.path.abspath(assets)
    return {
        "firecracker": os.path.join(root, "firecracker"),
        "kernel": os.path.join(root, "vmlinux"),
        "rootfs": os.path.join(root, "rootfs.ext4"),
        "guest_agent": os.path.join(root, "guest_agent"),
    }


def diagnose(assets: str = DEFAULT_ASSETS) -> dict:
    """What this machine is missing before a live RestoreState can run."""
    paths = asset_paths(assets)
    notes: list[str] = []
    kvm = have_kvm()
    if not kvm:
        notes.append("/dev/kvm 不能读写，Firecracker 起不来。")
    files = {k: os.path.isfile(p) for k, p in paths.items()}
    for key in ("firecracker", "kernel", "rootfs"):
        if not files[key]:
            notes.append(f"缺少 {paths[key]}")
    ready = kvm and files["firecracker"] and files["kernel"] and files["rootfs"]
    return {
        "kvm": kvm,
        "kvm_path": "/dev/kvm" if os.path.exists("/dev/kvm") else None,
        "assets_dir": os.path.abspath(assets),
        "files": files,
        "paths": paths,
        "ready": ready,
        "notes": notes,
    }


def _download(url: str, dest: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    print("download", url, "->", dest, file=sys.stderr)
    urllib.request.urlretrieve(url, dest)
    return dest


def compile_guest_agent(dest: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    subprocess.check_call(["gcc", "-static", "-O2", "-o", dest, AGENT_C])
    return dest


def _run(cmd: list[str]) -> None:
    subprocess.check_call(cmd)


def prepare_assets(assets: str = DEFAULT_ASSETS, *, download: bool = True) -> dict:
    """Put firecracker, vmlinux, guest_agent, and an alpine rootfs in `assets`."""
    os.makedirs(assets, exist_ok=True)
    paths = asset_paths(assets)
    compile_guest_agent(paths["guest_agent"])
    if download:
        if not os.path.isfile(paths["firecracker"]):
            tgz = os.path.join(assets, "fc.tgz")
            _download(FC_TGZ_URL, tgz)
            _run(["tar", "-xzf", tgz, "-C", assets])
            inner = os.path.join(assets, "release-v1.16.1-x86_64",
                                 "firecracker-v1.16.1-x86_64")
            if not os.path.isfile(inner):
                raise FileNotFoundError("firecracker binary missing inside tarball")
            shutil.copy2(inner, paths["firecracker"])
            os.chmod(paths["firecracker"], 0o755)
        if not os.path.isfile(paths["kernel"]):
            _download(KERNEL_URL, paths["kernel"])
        alpine = os.path.join(assets, "alpine.tar.gz")
        if not os.path.isfile(alpine):
            _download(ALPINE_URL, alpine)
    alpine = os.path.join(assets, "alpine.tar.gz")
    if not os.path.isfile(paths["rootfs"]):
        if not os.path.isfile(alpine):
            raise FileNotFoundError(
                f"need {alpine} (or pass --download) to build rootfs.ext4")
        _make_rootfs(paths["rootfs"], alpine, paths["guest_agent"])
    return diagnose(assets)


def _make_rootfs(dest: str, alpine_tar: str, agent: str) -> str:
    dest = os.path.abspath(dest)
    size_mb = 64
    _run(["dd", "if=/dev/zero", f"of={dest}", "bs=1M", f"count={size_mb}",
          "status=none"])
    _run(["mkfs.ext4", "-F", "-q", dest])
    mnt = tempfile.mkdtemp(prefix="trace-rootfs-")
    try:
        _run(["mount", "-o", "loop", dest, mnt])
        try:
            _run(["tar", "-xzf", alpine_tar, "-C", mnt])
            shutil.copy2(agent, os.path.join(mnt, "guest_agent"))
            os.chmod(os.path.join(mnt, "guest_agent"), 0o755)
            init = os.path.join(mnt, "init")
            with open(init, "w") as fh:
                fh.write("#!/bin/sh\nexec /guest_agent\n")
            os.chmod(init, 0o755)
        finally:
            _run(["umount", mnt])
    finally:
        os.rmdir(mnt)
    return dest


def _cfg(assets: str) -> FirecrackerConfig:
    paths = asset_paths(assets)
    return FirecrackerConfig(
        bin=paths["firecracker"],
        kernel=paths["kernel"],
        rootfs=paths["rootfs"],
    )


def check_restore(root: str, assets: str = DEFAULT_ASSETS, *,
                  use_uffd: bool = True) -> dict:
    """Boot, write a file, start sleep, CommitState, RestoreState, check both."""
    info = diagnose(assets)
    if not info["ready"]:
        return {
            "result": "SKIP",
            "reason": "; ".join(info["notes"]) or "not ready",
            "diagnose": info,
        }
    rt = Runtime(root, firecracker=_cfg(assets))
    live: list[str] = []
    try:
        started = rt.start_run("shell", backend="firecracker")
        sid = started["sandbox_id"]
        live.append(sid)
        wrote = rt.act(sid, "exec", {
            "cmd": f"echo {MARKER} > /tmp/marker; cat /tmp/marker",
        })
        if wrote.get("status") != "ok" or MARKER not in (
                wrote.get("observation") or {}).get("stdout", ""):
            return {
                "result": "FAIL",
                "reason": f"could not write /tmp/marker: {wrote}",
                "diagnose": info,
                "started": started,
            }
        bg = rt.act(sid, "exec", {"cmd": "sleep 120 & echo started"})
        if bg.get("status") != "ok":
            return {
                "result": "FAIL",
                "reason": f"could not start sleep: {bg}",
                "diagnose": info,
            }
        committed = rt.commit_state(sid)
        snap = committed["snapshot_uri"]
        for name in ("snapfile", "memfile", "header", "metadata.json"):
            path = os.path.join(snap, name)
            if not os.path.isfile(path):
                return {
                    "result": "FAIL",
                    "reason": f"missing snapshot file {path}",
                    "diagnose": info,
                    "committed": committed,
                }
        restored = rt.restore_state(committed["state_id"], use_uffd=use_uffd)
        rid = restored["sandbox_id"]
        live.append(rid)
        if rid == sid:
            return {
                "result": "FAIL",
                "reason": "RestoreState reused the same sandbox_id",
                "diagnose": info,
            }
        marker = rt.act(rid, "exec", {"cmd": "cat /tmp/marker"})
        stdout = (marker.get("observation") or {}).get("stdout", "")
        if marker.get("status") != "ok" or MARKER not in stdout:
            return {
                "result": "FAIL",
                "reason": f"/tmp/marker missing after RestoreState: {marker}",
                "diagnose": info,
                "restored": restored,
            }
        alive = rt.act(rid, "exec", {
            "cmd": "pgrep sleep >/dev/null && echo yes || echo no",
        })
        if "yes" not in (alive.get("observation") or {}).get("stdout", ""):
            return {
                "result": "FAIL",
                "reason": f"sleep process gone after RestoreState: {alive}",
                "diagnose": info,
                "restored": restored,
            }
        return {
            "result": "PASS",
            "reason": "RestoreState kept /tmp/marker and the sleep process",
            "diagnose": info,
            "parent_sandbox_id": sid,
            "restored_sandbox_id": rid,
            "state_id": committed["state_id"],
            "use_uffd": use_uffd,
        }
    except TraceError as e:
        return {
            "result": "FAIL",
            "reason": f"{e.code}: {e.message}",
            "diagnose": info,
        }
    finally:
        for sid in live:
            box = rt._boxes.get(sid)
            if box is not None and box.vm is not None:
                try:
                    box.vm.kill()
                except Exception:
                    pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets", default=DEFAULT_ASSETS)
    p.add_argument("--root", default="")
    p.add_argument("--diagnose", action="store_true")
    p.add_argument("--prepare", action="store_true")
    p.add_argument("--no-download", action="store_true")
    p.add_argument("--file-backend", action="store_true",
                   help="RestoreState with File mem backend (skip Uffd)")
    args = p.parse_args(argv)
    if args.prepare:
        got = prepare_assets(args.assets, download=not args.no_download)
        print(json.dumps(got, indent=2, ensure_ascii=False))
        return 0 if got.get("ready") or got.get("files", {}).get("rootfs") else 1
    if args.diagnose:
        print(json.dumps(diagnose(args.assets), indent=2, ensure_ascii=False))
        return 0
    root = args.root or tempfile.mkdtemp(prefix="trace-vm-replay-")
    got = check_restore(root, args.assets, use_uffd=not args.file_backend)
    print(json.dumps(got, indent=2, ensure_ascii=False))
    result = got.get("result")
    if result == "PASS":
        return 0
    if result == "SKIP":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

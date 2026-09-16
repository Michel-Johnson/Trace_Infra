"""Firecracker microVM driver. Standard library only.

Talks to the Firecracker HTTP API over a Unix socket. Guest commands go
through vsock to ew_examples/guest_agent.c. Nested KVM is required to boot;
this module still builds the snapshot files the phase-0 spec named.

Phase 4 loads memory through UFFD and stores the root disk as overlaybd
layers (shared RO base + private upper). Without ublk, the VMM sees a
materialized private file of the same layers.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from .store import TraceError
from .uffd import (
    DirtyMemfile, UffdHandler, capture_dirty_memfile, file_load_body,
    snapshot_load_body,
)

AGENT_PORT = 5252
BACKEND = "firecracker"


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 15.0):
        super().__init__("localhost", timeout=timeout)
        self._unix = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix)
        self.sock = sock


def _api(sock: str, method: str, path: str, body: dict | None = None,
         timeout: float = 15.0) -> tuple[int, str]:
    conn = _UnixHTTP(sock, timeout=timeout)
    payload = json.dumps(body).encode() if body is not None else b""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
        return resp.status, text
    finally:
        conn.close()


def _must(sock: str, method: str, path: str, body: dict | None = None,
          timeout: float = 15.0) -> None:
    status, text = _api(sock, method, path, body, timeout=timeout)
    if status >= 300:
        raise TraceError("VmmFailed", f"{method} {path} -> {status}: {text}")


@dataclass
class FirecrackerConfig:
    bin: str
    kernel: str
    rootfs: str
    mem_size_mib: int = 128
    vcpu_count: int = 1
    agent_port: int = AGENT_PORT


def _recvall(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise TraceError("VmmFailed", "guest agent closed the connection")
        buf += chunk
    return buf


def agent_exec(uds: str, cmd: str, timeout: float = 30.0) -> dict:
    """Talk the guest-agent framing to a Unix socket (host tests or vsock proxy)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(uds)
    try:
        payload = cmd.encode()
        sock.sendall(struct.pack("!I", len(payload)) + payload)
        code = struct.unpack("!I", _recvall(sock, 4))[0]
        nout = struct.unpack("!I", _recvall(sock, 4))[0]
        stdout = _recvall(sock, nout) if nout else b""
        nerr = struct.unpack("!I", _recvall(sock, 4))[0]
        stderr = _recvall(sock, nerr) if nerr else b""
        return {
            "exit": int(code),
            "stdout": stdout.decode("utf-8", "replace"),
            "stderr": stderr.decode("utf-8", "replace"),
        }
    finally:
        sock.close()


class FirecrackerVM:
    """One live Firecracker process. cwd is a jail with relative disk paths."""

    _cid = 3

    def __init__(self, cfg: FirecrackerConfig, jail: str):
        self.cfg = cfg
        self.jail = os.path.abspath(jail)
        os.makedirs(self.jail, exist_ok=True)
        self.api_sock = os.path.join(self.jail, "api.sock")
        self.log_path = os.path.join(self.jail, "fc.log")
        self.vsock_uds = os.path.join(self.jail, "vsock")
        self.rootfs_rel = "rootfs.ext4"
        self.rootfs_abs = os.path.join(self.jail, self.rootfs_rel)
        self.proc: subprocess.Popen | None = None
        self.guest_cid = FirecrackerVM._cid
        FirecrackerVM._cid += 1
        if FirecrackerVM._cid > 10000:
            FirecrackerVM._cid = 3
        self.uffd: UffdHandler | None = None

    def vsock_host(self) -> str:
        return f"{self.vsock_uds}_{self.cfg.agent_port}"

    def _spawn(self) -> None:
        for p in (self.api_sock, self.vsock_uds, self.vsock_host()):
            if os.path.exists(p):
                os.unlink(p)
        self.proc = subprocess.Popen(
            [self.cfg.bin, "--api-sock", self.api_sock,
             "--level", "Info", "--log-path", self.log_path],
            cwd=self.jail, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 5
        while time.time() < deadline:
            if os.path.exists(self.api_sock):
                return
            if self.proc.poll() is not None:
                raise TraceError("VmmFailed", f"firecracker exited: {self._log_tail()}")
            time.sleep(0.02)
        raise TraceError("VmmFailed", "api socket did not appear")

    def _log_tail(self) -> str:
        try:
            return open(self.log_path).read()[-1500:]
        except OSError:
            return ""

    def boot_from_rootfs(self, rootfs_src: str) -> None:
        if os.path.abspath(rootfs_src) != os.path.abspath(self.rootfs_abs):
            shutil.copy2(rootfs_src, self.rootfs_abs)
        self._spawn()
        _must(self.api_sock, "PUT", "/machine-config", {
            "vcpu_count": self.cfg.vcpu_count,
            "mem_size_mib": self.cfg.mem_size_mib,
            "smt": False,
            "track_dirty_pages": True,
        })
        _must(self.api_sock, "PUT", "/boot-source", {
            "kernel_image_path": os.path.abspath(self.cfg.kernel),
            "boot_args": ("console=ttyS0 reboot=k panic=1 pci=off "
                          "root=/dev/vda rw init=/init"),
        })
        _must(self.api_sock, "PUT", "/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": self.rootfs_rel,
            "is_root_device": True,
            "is_read_only": False,
        })
        _must(self.api_sock, "PUT", "/vsock", {
            "guest_cid": self.guest_cid,
            "uds_path": "vsock",
        })
        try:
            _must(self.api_sock, "PUT", "/actions",
                  {"action_type": "InstanceStart"}, timeout=8.0)
        except Exception as e:
            self.kill()
            raise TraceError(
                "VmmFailed",
                "InstanceStart failed or hung (needs working KVM). "
                f"{e}; log: {self._log_tail()}")
        self._wait_agent()

    def _put_load(self, body: dict) -> None:
        _must(self.api_sock, "PUT", "/snapshot/load", body, timeout=30.0)

    def _load_uffd(self, jail_mem: str) -> None:
        uffd_rel = "uffd.sock"
        handler = UffdHandler(os.path.join(self.jail, uffd_rel), jail_mem)
        handler.start()
        self.uffd = handler
        self._put_load(snapshot_load_body(uffd_rel, resume=True))

    def _load_file(self) -> None:
        self._put_load(file_load_body(resume=True))

    def _stop_uffd(self) -> None:
        if self.uffd is None:
            return
        try:
            self.uffd.stop()
        except Exception:
            pass
        self.uffd = None

    def load_snapshot(self, snapfile: str, memfile: str, rootfs: str,
                      *, use_uffd: bool = True, share_mem: bool = False) -> None:
        """Restore snapfile + memfile. Uffd first; packed memfile unpacks for File.

        Firecracker's File backend cannot read a TDIF packed memfile. If Uffd
        load fails, the packed file is unpacked to a dense dump and retried.
        `share_mem` keeps the forkd hardlink and always uses File.
        """
        if os.path.abspath(rootfs) != os.path.abspath(self.rootfs_abs):
            shutil.copy2(rootfs, self.rootfs_abs)
        jail_snap = os.path.join(self.jail, "snapfile")
        if os.path.abspath(snapfile) != os.path.abspath(jail_snap):
            shutil.copy2(snapfile, jail_snap)
        jail_mem = os.path.join(self.jail, "memfile")
        if share_mem:
            from .forkd import hardlink_cow
            if os.path.abspath(memfile) != os.path.abspath(jail_mem):
                hardlink_cow(memfile, jail_mem)
            use_uffd = False
        elif os.path.abspath(memfile) != os.path.abspath(jail_mem):
            shutil.copy2(memfile, jail_mem)
        self._spawn()
        uffd_err: Exception | None = None
        if use_uffd:
            try:
                self._load_uffd(jail_mem)
                self._wait_agent()
                return
            except Exception as e:
                uffd_err = e
                self._stop_uffd()
                self.kill()
                DirtyMemfile(jail_mem).materialize_dense(jail_mem)
                self._spawn()
        elif not share_mem:
            DirtyMemfile(jail_mem).materialize_dense(jail_mem)
        try:
            self._load_file()
            self._wait_agent()
        except Exception as file_err:
            self.kill()
            if uffd_err is None:
                raise
            raise TraceError(
                "VmmFailed",
                f"Uffd load failed ({uffd_err}); File fallback failed ({file_err})"
            ) from file_err

    def _wait_agent(self, seconds: float = 20.0) -> None:
        path = self.vsock_host()
        deadline = time.time() + seconds
        last = None
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise TraceError("VmmFailed", f"vm died during boot: {self._log_tail()}")
            if os.path.exists(path):
                try:
                    got = agent_exec(path, "echo ping", timeout=2.0)
                    if got.get("exit") == 0 and "ping" in got.get("stdout", ""):
                        return
                    last = got
                except Exception as e:
                    last = e
            time.sleep(0.1)
        raise TraceError("VmmFailed", f"guest agent not ready: {last}; {self._log_tail()}")

    def exec(self, cmd: str) -> dict:
        return agent_exec(self.vsock_host(), cmd)

    def pause(self) -> None:
        _must(self.api_sock, "PATCH", "/vm", {"state": "Paused"})

    def resume(self) -> None:
        _must(self.api_sock, "PATCH", "/vm", {"state": "Resumed"})

    def create_snapshot(self, snapfile: str, memfile: str, *, pack: bool = True) -> None:
        jail_snap = os.path.join(self.jail, "snapfile")
        jail_mem = os.path.join(self.jail, "memfile")
        _must(self.api_sock, "PUT", "/snapshot/create", {
            "snapshot_type": "Full",
            "snapshot_path": "snapfile",
            "mem_file_path": "memfile",
        }, timeout=60.0)
        os.makedirs(os.path.dirname(snapfile), exist_ok=True)
        shutil.copy2(jail_snap, snapfile)
        if pack:
            full_mem = memfile + ".full"
            try:
                shutil.copy2(jail_mem, full_mem)
                capture_dirty_memfile(full_mem, memfile)
            finally:
                if os.path.isfile(full_mem):
                    os.unlink(full_mem)
        else:
            shutil.copy2(jail_mem, memfile)

    def kill(self) -> None:
        if self.uffd is not None:
            try:
                self.uffd.stop()
            except Exception:
                pass
            self.uffd = None
        if self.proc is None:
            return
        try:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

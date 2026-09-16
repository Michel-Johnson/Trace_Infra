"""Phase 4: overlaybd-style disk layers and UFFD lazy memory restore.

    python3 -m pytest tests/test_trace_phase4.py -q
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ew_examples import FirecrackerConfig, Runtime, TraceError      # noqa: E402
from ew_examples.layers import LayeredDisk, ensure_base, have_ublk  # noqa: E402
from ew_examples.uffd import (                                      # noqa: E402
    DirtyMemfile, UffdHandler, capture_dirty_memfile, create_uffd,
    register_range, send_uffd, snapshot_load_body, write_dirty_memfile,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.environ.get("TRACE_FC_ASSETS", "/tmp/trace-fc-assets")
PAGE = 4096
PROT_READ, PROT_WRITE = 1, 2
MAP_PRIVATE, MAP_ANONYMOUS = 0x02, 0x20

_libc = ctypes.CDLL(None)
_libc.mmap.restype = ctypes.c_void_p
_libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, ctypes.c_long]
_libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_libc.memcpy.restype = ctypes.c_void_p
_libc.memcpy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]


def _mmap(n: int) -> int:
    addr = _libc.mmap(None, n, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0)
    assert addr != ctypes.c_void_p(-1).value
    return int(addr)


def _read(addr: int, n: int) -> bytes:
    """Touch guest pages without holding the GIL (ctypes.string_at would deadlock)."""
    buf = ctypes.create_string_buffer(n)
    _libc.memcpy(buf, ctypes.c_void_p(addr), n)
    return buf.raw


def _tiny_disk(path: str, n_blocks: int = 8) -> None:
    data = bytearray(n_blocks * PAGE)
    data[0:4] = b"BASE"
    data[PAGE:PAGE + 4] = b"TWO "
    open(path, "wb").write(data)


def test_two_sandboxes_share_readonly_base(tmp_path):
    """Acceptance: two sandboxes share the read-only layer."""
    template = str(tmp_path / "template.img")
    _tiny_disk(template)
    base = ensure_base(template, str(tmp_path / "layers" / "rootfs" / "base"))
    a = LayeredDisk(base, str(tmp_path / "a" / "upper"))
    b = LayeredDisk(base, str(tmp_path / "b" / "upper"))
    assert a.base_inode == b.base_inode
    assert a.base_inode == os.stat(base).st_ino
    orig = a.read_block(0)
    assert orig.startswith(b"BASE")
    a.write_block(0, b"AAAA" + b"\x00" * (PAGE - 4))
    assert a.read_block(0).startswith(b"AAAA")
    assert b.read_block(0).startswith(b"BASE")
    b.write_block(1, b"BBBB" + b"\x00" * (PAGE - 4))
    assert a.read_block(1).startswith(b"TWO ")
    assert b.read_block(1).startswith(b"BBBB")


def test_header_contains_overlaybd_block_map(tmp_path):
    template = str(tmp_path / "template.img")
    _tiny_disk(template, 4)
    base = ensure_base(template, str(tmp_path / "layers" / "rootfs" / "base"))
    disk = LayeredDisk(base, str(tmp_path / "snap" / "upper"))
    disk.write_block(2, b"UPPR" + b"\x00" * (PAGE - 4))
    header = {"vcpu_count": 1, "mem_size_mib": 128,
              "disk": disk.header_disk(base_id="rootfs", base_uri=base)}
    assert header["disk"]["kind"] == "overlaybd"
    blocks = header["disk"]["blocks"]
    assert blocks == [{"index": 2, "layer": "upper", "offset": 0}]
    path = str(tmp_path / "header")
    json.dump(header, open(path, "w"))
    loaded = json.load(open(path))
    again = LayeredDisk.from_snapshot(loaded, str(tmp_path / "snap"),
                                      str(tmp_path / "layers"))
    assert again.read_block(2).startswith(b"UPPR")
    assert again.read_block(0).startswith(b"BASE")


def test_snapshot_upper_smaller_than_full_rootfs(tmp_path):
    template = str(tmp_path / "template.img")
    _tiny_disk(template, 32)
    base = ensure_base(template, str(tmp_path / "layers" / "rootfs" / "base"))
    working = str(tmp_path / "working.img")
    disk = LayeredDisk(base, str(tmp_path / "upper"))
    disk.materialize(working)
    with open(working, "r+b") as fh:
        fh.seek(3 * PAGE)
        fh.write(b"DIRT" + b"\x00" * (PAGE - 4))
    info = disk.capture_from(working)
    upper_size = os.path.getsize(str(tmp_path / "upper"))
    assert info["n_dirty"] == 1
    assert upper_size < os.path.getsize(template)
    assert upper_size < 32 * PAGE


def test_dirty_memfile_smaller_than_full_dump(tmp_path):
    """Acceptance: snapshot volume is smaller than a full memory dump."""
    full = str(tmp_path / "full.bin")
    n_pages = 64
    blob = bytearray(n_pages * PAGE)
    blob[0:5] = b"page0"
    blob[7 * PAGE:7 * PAGE + 5] = b"page7"
    open(full, "wb").write(blob)
    packed = str(tmp_path / "memfile")
    info = capture_dirty_memfile(full, packed)
    assert info["kind"] == "dirty-pages"
    assert info["n_dirty"] == 2
    assert os.path.getsize(packed) < os.path.getsize(full)
    mem = DirtyMemfile(packed)
    assert mem.page(0).startswith(b"page0")
    assert mem.page(7).startswith(b"page7")
    assert mem.page(3) == b"\x00" * PAGE
    assert mem.bytes_read == 2 * PAGE
    assert mem.on_disk_bytes < mem.full_bytes


def test_uffd_first_command_before_full_memfile_read(tmp_path):
    """Acceptance: first touch does not wait for the whole memfile."""
    n_pages = 16
    pages = {0: b"FIRST" + b"\x00" * (PAGE - 5),
             9: b"NINTH" + b"\x00" * (PAGE - 5)}
    memfile = str(tmp_path / "memfile")
    write_dirty_memfile(memfile, pages, page_size=PAGE, n_pages=n_pages)
    sock_path = str(tmp_path / "uffd.sock")
    handler = UffdHandler(sock_path, memfile)
    handler.start()
    uffd = None
    addr = 0
    client = None
    length = n_pages * PAGE
    try:
        uffd = create_uffd(nonblock=False)
        addr = _mmap(length)
        register_range(uffd, addr, length)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.time() + 2
        while True:
            try:
                client.connect(sock_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.time() > deadline:
                    raise
                time.sleep(0.02)
        send_uffd(client, [{
            "base_host_virt_addr": addr,
            "size": length,
            "offset": 0,
            "page_size": PAGE,
        }], uffd)
        assert handler.serving.wait(timeout=2)
        if handler.failed:
            raise AssertionError(handler.failed)
        first = _read(addr, 5)
        assert first == b"FIRST"
        assert handler.pages_served == 1
        assert handler.mem.bytes_read == PAGE
        assert handler.mem.bytes_read < handler.mem.full_bytes
        ninth = _read(addr + 9 * PAGE, 5)
        assert ninth == b"NINTH"
        assert handler.pages_served == 2
        assert handler.mem.bytes_read == 2 * PAGE
        empty = _read(addr + PAGE, 4)
        assert empty == b"\x00\x00\x00\x00"
        assert handler.pages_served == 3
    finally:
        handler.stop()
        if uffd is not None:
            os.close(uffd)
        if addr:
            _libc.munmap(addr, length)
        try:
            client.close()
        except Exception:
            pass


def test_snapshot_load_body_uses_uffd():
    body = snapshot_load_body("uffd.sock")
    assert body["mem_backend"]["backend_type"] == "Uffd"
    assert body["mem_backend"]["backend_path"] == "uffd.sock"
    assert body["resume_vm"] is True


def test_ublk_absent_falls_back_to_file(tmp_path):
    template = str(tmp_path / "template.img")
    _tiny_disk(template, 4)
    base = ensure_base(template, str(tmp_path / "layers" / "rootfs" / "base"))
    disk = LayeredDisk(base, str(tmp_path / "upper"))
    dest = str(tmp_path / "rootfs.ext4")
    path = disk.backing_for_vmm(dest)
    if have_ublk():
        pytest.skip("ublk is present; file fallback not used")
    with pytest.raises(TraceError) as e:
        disk.attach_ublk()
    assert e.value.code == "VmmFailed"
    assert os.path.isfile(path)
    assert path == dest
    assert open(path, "rb").read(4) == b"BASE"


def test_start_run_still_defaults_to_episode(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("counter", 3)
    assert started["backend"] == "episode"


def _have_kvm() -> bool:
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def _fc_cfg():
    bin_path = os.path.join(ASSETS, "firecracker")
    kernel = os.path.join(ASSETS, "vmlinux")
    rootfs = os.path.join(ASSETS, "rootfs.ext4")
    if not (os.path.isfile(bin_path) and os.path.isfile(kernel)
            and os.path.isfile(rootfs)):
        pytest.skip("firecracker assets missing under " + ASSETS)
    return FirecrackerConfig(bin=bin_path, kernel=kernel, rootfs=rootfs)


def test_restore_uffd_and_shared_base_live(tmp_path):
    """Live Firecracker path. Skips when nested KVM cannot boot a VM."""
    if not _have_kvm():
        pytest.skip("no /dev/kvm")
    cfg = _fc_cfg()
    rt = Runtime(str(tmp_path), firecracker=cfg)
    try:
        started = rt.start_run("shell", backend="firecracker")
    except TraceError as e:
        if e.code == "VmmFailed":
            pytest.skip(e.message)
        raise
    try:
        other = rt.start_run("shell", backend="firecracker")
    except TraceError as e:
        if e.code == "VmmFailed":
            pytest.skip(e.message)
        raise
    a = rt._box(started["sandbox_id"])
    b = rt._box(other["sandbox_id"])
    assert a.disk is not None and b.disk is not None
    assert a.disk.base_inode == b.disk.base_inode
    sid = started["sandbox_id"]
    wrote = rt.act(sid, "exec", {"cmd": "echo keepme > /tmp/marker; cat /tmp/marker"})
    assert wrote["status"] == "ok"
    committed = rt.commit_state(sid)
    snap = committed["snapshot_uri"]
    assert os.path.isfile(os.path.join(snap, "upper"))
    assert os.path.isfile(os.path.join(snap, "memfile"))
    assert os.path.isfile(os.path.join(snap, "header"))
    assert not os.path.isfile(os.path.join(snap, "rootfs"))
    header = json.load(open(os.path.join(snap, "header")))
    assert header["disk"]["kind"] == "overlaybd"
    assert header["memory"]["kind"] == "dirty-pages"
    mem_size = os.path.getsize(os.path.join(snap, "memfile"))
    assert mem_size < cfg.mem_size_mib * 1024 * 1024
    restored = rt.restore_state(committed["state_id"])
    assert restored["sandbox_id"] != sid
    marker = rt.act(restored["sandbox_id"], "exec", {"cmd": "cat /tmp/marker"})
    assert marker["status"] == "ok"
    assert "keepme" in marker["observation"]["stdout"]
    vm = rt._box(restored["sandbox_id"]).vm
    assert vm is not None and vm.uffd is not None
    rt._box(sid).vm.kill()
    rt._box(other["sandbox_id"]).vm.kill()
    rt._box(restored["sandbox_id"]).vm.kill()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

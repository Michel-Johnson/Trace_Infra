"""Firecracker UFFD page-fault restore. Standard library only.

Firecracker PUT /snapshot/load with backend_type Uffd connects to a Unix socket
and sends JSON GuestRegionUffdMapping[] plus the userfaultfd via SCM_RIGHTS.
This module is that handler: UFFDIO_COPY one page at a time from a packed
dirty-page memfile. Tests drive it without a microVM.
"""
from __future__ import annotations

import array
import ctypes
import ctypes.util
import json
import os
import select
import shutil
import socket
import struct
import threading
from typing import Any, Callable

from .store import TraceError

MAGIC = b"TDIF"
VERSION = 1
PAGE_SIZE = 4096
KIND = "dirty-pages"

UFFD_API = 0xAA
UFFD_USER_MODE_ONLY = 1
UFFDIO_REGISTER_MODE_MISSING = 1
UFFD_EVENT_PAGEFAULT = 0x12
SYS_userfaultfd = 323  # x86_64

# ioctl numbers from linux/userfaultfd.h (verified against sizeof on this kernel)
UFFDIO_API = 0xC018AA3F
UFFDIO_REGISTER = 0xC020AA00
UFFDIO_COPY = 0xC028AA03

_O_CLOEXEC = 0o2000000
_O_NONBLOCK = 0o4000

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.syscall.restype = ctypes.c_long
_libc.syscall.argtypes = [ctypes.c_long, ctypes.c_long]


class _uffdio_api(ctypes.Structure):
    _fields_ = [("api", ctypes.c_uint64), ("features", ctypes.c_uint64),
                ("ioctls", ctypes.c_uint64)]


class _uffdio_range(ctypes.Structure):
    _fields_ = [("start", ctypes.c_uint64), ("len", ctypes.c_uint64)]


class _uffdio_register(ctypes.Structure):
    _fields_ = [("range", _uffdio_range), ("mode", ctypes.c_uint64),
                ("ioctls", ctypes.c_uint64)]


class _uffdio_copy(ctypes.Structure):
    _fields_ = [("dst", ctypes.c_uint64), ("src", ctypes.c_uint64),
                ("len", ctypes.c_uint64), ("mode", ctypes.c_uint64),
                ("copy", ctypes.c_int64)]


def create_uffd(*, nonblock: bool = True) -> int:
    """userfaultfd(2) with UFFD_USER_MODE_ONLY, then UFFDIO_API."""
    flags = _O_CLOEXEC | UFFD_USER_MODE_ONLY
    if nonblock:
        flags |= _O_NONBLOCK
    fd = int(_libc.syscall(SYS_userfaultfd, flags))
    if fd < 0:
        err = ctypes.get_errno()
        raise TraceError("VmmFailed", f"userfaultfd syscall failed: {os.strerror(err)}")
    import fcntl
    api = _uffdio_api(UFFD_API, 0, 0)
    try:
        fcntl.ioctl(fd, UFFDIO_API, api)
    except OSError as e:
        os.close(fd)
        raise TraceError("VmmFailed", f"UFFDIO_API failed: {e}") from e
    return fd


def register_range(uffd: int, addr: int, length: int) -> None:
    import fcntl
    reg = _uffdio_register()
    reg.range.start = addr
    reg.range.len = length
    reg.mode = UFFDIO_REGISTER_MODE_MISSING
    fcntl.ioctl(uffd, UFFDIO_REGISTER, reg)


def uffdio_copy(uffd: int, dst: int, src: int, length: int) -> int:
    import fcntl
    cp = _uffdio_copy()
    cp.dst = dst
    cp.src = src
    cp.len = length
    cp.mode = 0
    fcntl.ioctl(uffd, UFFDIO_COPY, cp)
    return int(cp.copy)


def write_dirty_memfile(path: str, pages: dict[int, bytes], *,
                        page_size: int, n_pages: int) -> dict[str, Any]:
    """Pack dirty pages. Missing indices restore as zeros."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    ordered = sorted(pages.items())
    with open(path, "wb") as fh:
        fh.write(MAGIC)
        fh.write(struct.pack("<IIQQ", VERSION, page_size, n_pages, len(ordered)))
        for idx, _data in ordered:
            fh.write(struct.pack("<Q", idx))
        for _idx, data in ordered:
            if len(data) != page_size:
                data = (data + b"\x00" * page_size)[:page_size]
            fh.write(data)
    return {
        "kind": KIND,
        "page_size": page_size,
        "len": n_pages * page_size,
        "n_pages": n_pages,
        "n_dirty": len(ordered),
        "dirty": [idx for idx, _ in ordered],
    }


def capture_dirty_memfile(current: str, dest: str, *,
                          page_size: int = PAGE_SIZE,
                          baseline: Callable[[int], bytes] | None = None) -> dict[str, Any]:
    """Diff `current` (full dump) against zeros or `baseline(page_index)`."""
    size = os.path.getsize(current)
    n_pages = (size + page_size - 1) // page_size
    dirty: dict[int, bytes] = {}
    zero = b"\x00" * page_size
    with open(current, "rb") as fh:
        for i in range(n_pages):
            page = fh.read(page_size)
            if len(page) < page_size:
                page = page + b"\x00" * (page_size - len(page))
            old = baseline(i) if baseline is not None else zero
            if page != old:
                dirty[i] = page
    return write_dirty_memfile(dest, dirty, page_size=page_size, n_pages=n_pages)


class DirtyMemfile:
    """Packed dirty-page memfile. Reads count bytes actually fetched."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.bytes_read = 0
        with open(self.path, "rb") as fh:
            magic = fh.read(4)
            if magic != MAGIC:
                # Full dump from Firecracker (phase 3). Serve it as dense pages.
                size = os.path.getsize(self.path)
                self.page_size = PAGE_SIZE
                self.n_pages = (size + PAGE_SIZE - 1) // PAGE_SIZE
                self._index = {i: i * PAGE_SIZE for i in range(self.n_pages)}
                self.packed = False
                return
            _ver, page_size, n_pages, n_dirty = struct.unpack("<IIQQ", fh.read(24))
            self.page_size = page_size
            self.n_pages = n_pages
            indices = list(struct.unpack("<" + "Q" * n_dirty, fh.read(8 * n_dirty)))
            data_off = 28 + 8 * n_dirty
            self._index = {idx: data_off + k * page_size for k, idx in enumerate(indices)}
            self.packed = True

    @property
    def full_bytes(self) -> int:
        return self.n_pages * self.page_size

    @property
    def on_disk_bytes(self) -> int:
        return os.path.getsize(self.path)

    def page(self, index: int) -> bytes:
        off = self._index.get(index)
        if off is None:
            return b"\x00" * self.page_size
        with open(self.path, "rb") as fh:
            fh.seek(off)
            data = fh.read(self.page_size)
        self.bytes_read += len(data)
        if len(data) < self.page_size:
            data = data + b"\x00" * (self.page_size - len(data))
        return data

    def header_memory(self) -> dict[str, Any]:
        return {
            "kind": KIND,
            "page_size": self.page_size,
            "len": self.full_bytes,
            "n_pages": self.n_pages,
            "n_dirty": len(self._index) if self.packed else self.n_pages,
            "dirty": sorted(self._index) if self.packed else None,
        }

    def materialize_dense(self, dest: str) -> str:
        """Write a contiguous RAM dump. Firecracker's File backend needs this.

        Packed TDIF memfiles only work with the Uffd handler. Same path as dest
        is replaced atomically after the dense file is fully written.
        """
        dest = os.path.abspath(dest)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        if not self.packed:
            if os.path.abspath(self.path) != dest:
                shutil.copy2(self.path, dest)
            return dest
        tmp = dest + ".dense.tmp"
        zero = b"\x00" * self.page_size
        with open(tmp, "wb") as fh:
            for i in range(self.n_pages):
                off = self._index.get(i)
                if off is None:
                    fh.write(zero)
                else:
                    fh.write(self.page(i))
        os.replace(tmp, dest)
        return dest


def send_uffd(sock: socket.socket, mappings: list[dict], uffd: int) -> None:
    payload = json.dumps(mappings).encode()
    fds = array.array("i", [int(uffd)])
    sock.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])


def recv_uffd(sock: socket.socket, retries: int = 8) -> tuple[list[dict], int]:
    import time
    last = b""
    for _ in range(retries):
        msg, anc, _flags, _addr = sock.recvmsg(65536, socket.CMSG_SPACE(4))
        last = msg or last
        fds: list[int] = []
        for level, typ, data in anc:
            if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                arr = array.array("i")
                arr.frombytes(data[:len(data) - (len(data) % arr.itemsize)])
                fds.extend(arr)
        if last and fds:
            mappings = json.loads(last.decode())
            return mappings, int(fds[0])
        time.sleep(0.05)
    raise TraceError("VmmFailed", "did not receive GuestRegionUffdMapping + UFFD")


def snapshot_load_body(uffd_rel: str = "uffd.sock", *,
                       resume: bool = True) -> dict:
    """PUT /snapshot/load body when memory is served through UFFD."""
    return {
        "snapshot_path": "snapfile",
        "mem_backend": {"backend_type": "Uffd", "backend_path": uffd_rel},
        "resume_vm": resume,
        "track_dirty_pages": True,
    }


def file_load_body(*, resume: bool = True) -> dict:
    """PUT /snapshot/load body when memory is a dense memfile on disk."""
    return {
        "snapshot_path": "snapfile",
        "mem_backend": {"backend_type": "File", "backend_path": "memfile"},
        "resume_vm": resume,
        "track_dirty_pages": True,
    }


class UffdHandler:
    """Listen on a Unix socket, take FC's uffd, copy pages on fault."""

    def __init__(self, sock_path: str, memfile: str):
        self.sock_path = os.path.abspath(sock_path)
        self.mem = DirtyMemfile(memfile)
        self.pages_served = 0
        self.serving = threading.Event()
        self.failed: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener: socket.socket | None = None

    def start(self) -> None:
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        os.makedirs(os.path.dirname(self.sock_path) or ".", exist_ok=True)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.sock_path)
        self._listener.listen(1)
        self._listener.settimeout(0.2)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if os.path.exists(self.sock_path):
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                assert self._listener is not None
                try:
                    conn, _ = self._listener.accept()
                except socket.timeout:
                    continue
                try:
                    self._serve(conn)
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
                break
        except Exception as e:
            self.failed = str(e)
            self.serving.set()

    def _serve(self, conn: socket.socket) -> None:
        mappings, uffd = recv_uffd(conn)
        self.serving.set()
        regions = []
        for m in mappings:
            regions.append({
                "base": int(m["base_host_virt_addr"]),
                "size": int(m["size"]),
                "offset": int(m.get("offset") or 0),
                "page_size": int(m.get("page_size") or self.mem.page_size),
            })
        while not self._stop.is_set():
            r, _, _ = select.select([uffd], [], [], 0.1)
            if not r:
                continue
            try:
                msg = os.read(uffd, 32)
            except BlockingIOError:
                continue
            except OSError:
                break
            if len(msg) < 32:
                break
            if msg[0] != UFFD_EVENT_PAGEFAULT:
                continue
            _flags, address = struct.unpack_from("<QQ", msg, 8)
            self._fill(uffd, regions, address)

    def _fill(self, uffd: int, regions: list[dict], address: int) -> None:
        for region in regions:
            base = region["base"]
            size = region["size"]
            if address < base or address >= base + size:
                continue
            page_size = region["page_size"]
            dst = address & ~(page_size - 1)
            file_off = region["offset"] + (dst - base)
            index = file_off // page_size
            page = self.mem.page(index)
            buf = ctypes.create_string_buffer(page, page_size)
            try:
                uffdio_copy(uffd, dst, ctypes.addressof(buf), page_size)
            except OSError as e:
                # EEXIST: already filled. Ignore.
                if e.errno != 17:
                    raise
            self.pages_served += 1
            return
        raise TraceError("VmmFailed", f"UFFD fault {address:#x} outside mappings")

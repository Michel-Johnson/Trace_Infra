"""Overlaybd-style layered root disk. Standard library only.

A shared read-only base file plus a private upper of dirty blocks. That is the
overlaybd model: two sandboxes open the same base inode and write their own
upper. If `/dev/ublk-control` (or overlaybd-ublk) is present, attach_ublk()
exposes the view as a ublk device. This host usually has neither, so Firecracker
gets a private materialized file built from the same layers.
"""
from __future__ import annotations

import os
import shutil
import struct
from typing import Any

from .store import TraceError

MAGIC = b"OVBD"
VERSION = 1
BLOCK_SIZE = 4096
KIND = "overlaybd"


def have_ublk() -> bool:
    if os.access("/dev/ublk-control", os.R_OK | os.W_OK):
        return True
    return shutil.which("overlaybd-ublk") is not None or shutil.which("ublk") is not None


def ensure_base(template: str, dest: str) -> str:
    """Put a shared RO copy of `template` at `dest`. Prefer a hard link."""
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.isfile(dest):
        return dest
    try:
        os.link(os.path.abspath(template), dest)
    except OSError:
        shutil.copy2(template, dest)
        try:
            os.chmod(dest, 0o444)
        except OSError:
            pass
    return dest


def _header(path: str) -> tuple[int, int, int, int]:
    with open(path, "rb") as fh:
        magic = fh.read(4)
        if magic != MAGIC:
            raise TraceError("VmmFailed", f"not an overlaybd upper: {path}")
        version, block_size, n_total, n_dirty = struct.unpack("<IIQQ", fh.read(24))
        if version != VERSION:
            raise TraceError("VmmFailed", f"upper version {version}")
        return version, block_size, n_total, n_dirty


def write_upper(path: str, *, block_size: int, n_total: int,
                dirty: list[tuple[int, bytes]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(MAGIC)
        fh.write(struct.pack("<IIQQ", VERSION, block_size, n_total, len(dirty)))
        for idx, _data in dirty:
            fh.write(struct.pack("<Q", idx))
        for _idx, data in dirty:
            if len(data) != block_size:
                data = data + b"\x00" * (block_size - len(data))
            fh.write(data[:block_size])


def capture_upper(working: str, base: str, dest: str,
                  block_size: int = BLOCK_SIZE) -> dict[str, Any]:
    """Diff `working` against `base`. Write packed dirty blocks to `dest`."""
    size = os.path.getsize(working)
    n_total = (size + block_size - 1) // block_size
    dirty: list[tuple[int, bytes]] = []
    with open(working, "rb") as wfh, open(base, "rb") as bfh:
        for i in range(n_total):
            wdata = wfh.read(block_size)
            bdata = bfh.read(block_size)
            if len(wdata) < block_size:
                wdata = wdata + b"\x00" * (block_size - len(wdata))
            if len(bdata) < block_size:
                bdata = bdata + b"\x00" * (block_size - len(bdata))
            if wdata != bdata:
                dirty.append((i, wdata))
    write_upper(dest, block_size=block_size, n_total=n_total, dirty=dirty)
    return {
        "kind": KIND,
        "block_size": block_size,
        "size": n_total * block_size,
        "n_dirty": len(dirty),
        "blocks": [
            {"index": i, "layer": "upper", "offset": k * block_size}
            for k, (i, _data) in enumerate(dirty)
        ],
    }


class LayeredDisk:
    """One writable view: shared base inode + private upper."""

    def __init__(self, base_path: str, upper_path: str, *,
                 block_size: int = BLOCK_SIZE, size: int | None = None):
        self.base_path = os.path.abspath(base_path)
        self.upper_path = os.path.abspath(upper_path)
        self.block_size = block_size
        self._index: dict[int, int] = {}
        self._n_total = 0
        if os.path.isfile(self.base_path):
            base_size = os.path.getsize(self.base_path)
        else:
            base_size = 0
        if os.path.isfile(self.upper_path) and os.path.getsize(self.upper_path) > 0:
            self._load_upper()
        else:
            self._n_total = (base_size + block_size - 1) // block_size
        if size is not None:
            self._n_total = max(self._n_total, (size + block_size - 1) // block_size)

    @property
    def base_inode(self) -> int:
        return os.stat(self.base_path).st_ino

    @property
    def size(self) -> int:
        return self._n_total * self.block_size

    def _load_upper(self) -> None:
        _version, block_size, n_total, n_dirty = _header(self.upper_path)
        self.block_size = block_size
        self._n_total = n_total
        with open(self.upper_path, "rb") as fh:
            fh.seek(28)
            indices = list(struct.unpack("<" + "Q" * n_dirty, fh.read(8 * n_dirty)))
            data_off = 28 + 8 * n_dirty
            self._index = {idx: data_off + k * block_size for k, idx in enumerate(indices)}

    def read_block(self, index: int) -> bytes:
        off = self._index.get(index)
        if off is not None:
            with open(self.upper_path, "rb") as fh:
                fh.seek(off)
                data = fh.read(self.block_size)
            return data + b"\x00" * (self.block_size - len(data))
        with open(self.base_path, "rb") as fh:
            fh.seek(index * self.block_size)
            data = fh.read(self.block_size)
        return data + b"\x00" * (self.block_size - len(data))

    def write_block(self, index: int, data: bytes) -> None:
        if len(data) != self.block_size:
            data = (data + b"\x00" * self.block_size)[:self.block_size]
        dirty = []
        for i in range(max(self._n_total, index + 1)):
            if i == index:
                dirty.append((i, data))
            elif i in self._index:
                dirty.append((i, self.read_block(i)))
        self._n_total = max(self._n_total, index + 1)
        write_upper(self.upper_path, block_size=self.block_size,
                    n_total=self._n_total, dirty=dirty)
        self._load_upper()

    def header_disk(self, *, base_id: str, base_uri: str,
                    extra: dict | None = None) -> dict[str, Any]:
        data_off = 28 + 8 * len(self._index)
        blocks = [
            {"index": idx, "layer": "upper", "offset": off - data_off}
            for idx, off in sorted(self._index.items())
        ]
        out = {
            "kind": KIND,
            "block_size": self.block_size,
            "size": self.size,
            "base_id": base_id,
            "base_uri": base_uri,
            "upper": os.path.basename(self.upper_path),
            "n_dirty": len(self._index),
            "blocks": blocks,
        }
        if extra:
            out.update(extra)
        return out

    def materialize(self, dest: str) -> str:
        """Private contiguous file for Firecracker when ublk is missing."""
        dest = os.path.abspath(dest)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copy2(self.base_path, dest)
        if not self._index:
            return dest
        with open(dest, "r+b") as fh:
            for idx, off in self._index.items():
                with open(self.upper_path, "rb") as ufh:
                    ufh.seek(off)
                    data = ufh.read(self.block_size)
                fh.seek(idx * self.block_size)
                fh.write(data)
        return dest

    def capture_from(self, working: str) -> dict[str, Any]:
        info = capture_upper(working, self.base_path, self.upper_path, self.block_size)
        self._load_upper()
        return info

    def attach_ublk(self) -> str:
        """Expose this view as a ublk device. Requires overlaybd-ublk or ublk."""
        if not have_ublk():
            raise TraceError(
                "VmmFailed",
                "ublk is not available on this host; materialize() is the fallback")
        raise TraceError(
            "VmmFailed",
            "overlaybd-ublk is present but this pack does not drive it yet")

    def backing_for_vmm(self, dest: str) -> str:
        if have_ublk():
            try:
                return self.attach_ublk()
            except TraceError:
                pass
        return self.materialize(dest)

    @classmethod
    def from_snapshot(cls, header: dict, snap_dir: str, layers_root: str) -> LayeredDisk:
        disk = header.get("disk") or {}
        if disk.get("kind") != KIND:
            raise TraceError("VmmFailed", "snapshot header has no overlaybd disk map")
        base_id = disk.get("base_id") or "rootfs"
        base = os.path.join(layers_root, base_id, "base")
        if not os.path.isfile(base):
            uri = disk.get("base_uri")
            if uri and os.path.isfile(uri):
                base = uri
            else:
                raise TraceError("StateNotFound", f"missing shared base {base}")
        upper = os.path.join(snap_dir, disk.get("upper") or "upper")
        return cls(base, upper, block_size=int(disk.get("block_size") or BLOCK_SIZE),
                   size=int(disk.get("size") or 0) or None)

"""forkd-style BRANCH. Standard library only.

Children are separate views of one frozen parent image. Guest RAM is a dense
memfile; each child `mmap`s it `MAP_PRIVATE` so the kernel copy-on-write
shares clean pages. That is not N full copies. Disk stays overlaybd: same
read-only base inode, each child a private upper cloned at fork time.

Vanilla Firecracker File-backend restore already maps the memfile private.
This module freezes one image and hard-links it into each child jail.
"""
from __future__ import annotations

import mmap
import os
import shutil
from dataclasses import dataclass

from .layers import LayeredDisk
from .store import TraceError


def hardlink_cow(src: str, dst: str) -> str:
    """Share one inode. Copying would break CoW, so that is a hard error."""
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if os.path.lexists(dst):
        os.unlink(dst)
    try:
        os.link(src, dst)
    except OSError as e:
        raise TraceError(
            "BranchFailed",
            f"CoW hardlink failed for {dst}: {e}") from e
    return dst


def map_private(path: str, size: int | None = None) -> mmap.mmap:
    """MAP_PRIVATE / ACCESS_COPY: writes stay in this map, file unchanged."""
    path = os.path.abspath(path)
    size = int(size or os.path.getsize(path))
    fd = os.open(path, os.O_RDONLY)
    try:
        return mmap.mmap(fd, size, access=mmap.ACCESS_COPY)
    finally:
        os.close(fd)


def write_dense(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return os.path.abspath(path)


def inode(path: str) -> int:
    return os.stat(path).st_ino


def nlink(path: str) -> int:
    return os.stat(path).st_nlink


def on_disk_bytes(path: str) -> int:
    return os.stat(path).st_blocks * 512


@dataclass
class ParentImage:
    """One frozen BRANCH image. N children hard-link `memfile`."""

    dir: str
    memfile: str
    snapfile: str | None = None
    disk: LayeredDisk | None = None

    @property
    def mem_inode(self) -> int:
        return inode(self.memfile)

    @property
    def mem_size(self) -> int:
        return os.path.getsize(self.memfile)


def freeze_memfile(src: str, dest: str) -> str:
    """One copy of the dense RAM image. Children then hard-link `dest`."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    shutil.copy2(src, dest)
    return os.path.abspath(dest)


def freeze_disk(parent: LayeredDisk, dest_upper: str,
                working: str | None = None) -> LayeredDisk:
    """Capture parent dirty blocks into one upper, then children clone it."""
    frozen = LayeredDisk(parent.base_path, dest_upper,
                         block_size=parent.block_size, size=parent.size)
    if working and os.path.isfile(working):
        frozen.capture_from(working)
    elif os.path.isfile(parent.upper_path) and os.path.getsize(parent.upper_path) > 0:
        os.makedirs(os.path.dirname(os.path.abspath(dest_upper)) or ".", exist_ok=True)
        shutil.copy2(parent.upper_path, dest_upper)
        frozen._load_upper()
    return frozen


def fork_disk(frozen: LayeredDisk, child_upper: str) -> LayeredDisk:
    """Same base inode; private upper starting at the freeze point."""
    os.makedirs(os.path.dirname(os.path.abspath(child_upper)) or ".", exist_ok=True)
    if os.path.isfile(frozen.upper_path) and os.path.getsize(frozen.upper_path) > 0:
        shutil.copy2(frozen.upper_path, child_upper)
    return LayeredDisk(frozen.base_path, child_upper,
                       block_size=frozen.block_size, size=frozen.size)


@dataclass
class ChildJail:
    sandbox_id: str
    jail: str
    memfile: str
    snapfile: str | None
    disk: LayeredDisk | None


def place_child(image: ParentImage, sandbox_id: str, jail: str) -> ChildJail:
    """Build one child jail that CoW-shares the frozen memfile inode."""
    os.makedirs(jail, exist_ok=True)
    mem = hardlink_cow(image.memfile, os.path.join(jail, "memfile"))
    snap = None
    if image.snapfile and os.path.isfile(image.snapfile):
        snap = os.path.join(jail, "snapfile")
        shutil.copy2(image.snapfile, snap)
    disk = None
    if image.disk is not None:
        disk = fork_disk(image.disk, os.path.join(jail, "upper"))
    return ChildJail(sandbox_id=sandbox_id, jail=jail, memfile=mem,
                     snapfile=snap, disk=disk)

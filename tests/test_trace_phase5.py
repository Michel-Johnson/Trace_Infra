"""Phase 5: forkd-style Branch with MAP_PRIVATE CoW memory.

    python3 -m pytest tests/test_trace_phase5.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ew_examples import FirecrackerConfig, Runtime, TraceError      # noqa: E402
from ew_examples.forkd import (                                     # noqa: E402
    ParentImage, freeze_disk, fork_disk, hardlink_cow, inode, map_private,
    nlink, on_disk_bytes, place_child, write_dense,
)
from ew_examples.layers import LayeredDisk, ensure_base             # noqa: E402

PAGE = 4096
ASSETS = os.environ.get("TRACE_FC_ASSETS", "/tmp/trace-fc-assets")


def _tiny_disk(path: str, n_blocks: int = 8) -> None:
    data = bytearray(n_blocks * PAGE)
    data[0:4] = b"BASE"
    open(path, "wb").write(data)


def test_map_private_writes_do_not_change_sibling_or_file(tmp_path):
    """Acceptance: two MAP_PRIVATE views diverge; the frozen file does not."""
    blob = b"PARENT" + b"\x00" * (PAGE - 6) + os.urandom(7 * PAGE)
    mem = write_dense(str(tmp_path / "memfile"), blob)
    a = map_private(mem)
    b = map_private(mem)
    try:
        assert a[:6] == b"PARENT"
        assert b[:6] == b"PARENT"
        a[0:6] = b"CHILD0"
        assert a[:6] == b"CHILD0"
        assert b[:6] == b"PARENT"
        assert open(mem, "rb").read(6) == b"PARENT"
        b[0:6] = b"CHILD1"
        assert a[:6] == b"CHILD0"
        assert b[:6] == b"CHILD1"
        assert open(mem, "rb").read(6) == b"PARENT"
    finally:
        a.close()
        b.close()


def test_n_children_share_one_memfile_inode(tmp_path):
    """Acceptance: RAM on disk is one image, not N full copies."""
    n = 3
    size = 32 * PAGE
    mem = write_dense(str(tmp_path / "memfile"), b"X" * size)
    image = ParentImage(dir=str(tmp_path), memfile=mem)
    children = []
    for i in range(n):
        sid = f"child{i}"
        jail = str(tmp_path / sid)
        children.append(place_child(image, sid, jail))
    inodes = {inode(c.memfile) for c in children}
    inodes.add(inode(mem))
    assert len(inodes) == 1
    assert nlink(mem) == n + 1
    assert on_disk_bytes(mem) < n * size
    for c in children:
        assert inode(c.memfile) == inode(mem)


def test_hardlink_cow_refuses_to_copy(tmp_path):
    src = write_dense(str(tmp_path / "a" / "mem"), b"x" * PAGE)
    dst = str(tmp_path / "b" / "mem")
    hardlink_cow(src, dst)
    assert inode(src) == inode(dst)


def test_fork_disk_shares_base_and_isolates_upper(tmp_path):
    template = str(tmp_path / "template.img")
    _tiny_disk(template)
    base = ensure_base(template, str(tmp_path / "layers" / "rootfs" / "base"))
    parent = LayeredDisk(base, str(tmp_path / "parent" / "upper"))
    parent.write_block(0, b"PAR0" + b"\x00" * (PAGE - 4))
    frozen = freeze_disk(parent, str(tmp_path / "fork" / "upper"))
    ca = fork_disk(frozen, str(tmp_path / "c0" / "upper"))
    cb = fork_disk(frozen, str(tmp_path / "c1" / "upper"))
    assert ca.base_inode == cb.base_inode == parent.base_inode
    assert ca.read_block(0).startswith(b"PAR0")
    ca.write_block(0, b"AAA0" + b"\x00" * (PAGE - 4))
    assert cb.read_block(0).startswith(b"PAR0")
    assert parent.read_block(0).startswith(b"PAR0")


def test_episode_branch_still_works(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("counter", 3)
    sid = started["sandbox_id"]
    rt.act(sid, "peek", {})
    got = rt.branch(sid, 2)
    assert len(got["children"]) == 2
    assert all("sandbox_id" in c for c in got["children"])


def test_firecracker_branch_without_vm_is_branch_failed(tmp_path):
    rt = Runtime(str(tmp_path))
    from ew_examples.trace import FC_BACKEND, _Box
    rt._boxes["p"] = _Box(sandbox_id="p", run_id="r", backend=FC_BACKEND)
    with pytest.raises(TraceError) as e:
        rt.branch("p", 2)
    assert e.value.code == "BranchFailed"
    assert "forkd" in e.value.message


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


def test_live_branch_children_exec_independently(tmp_path):
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
    sid = started["sandbox_id"]
    rt.act(sid, "exec", {"cmd": "echo parent > /tmp/who"})
    got = rt.branch(sid, 2)
    kids = [c for c in got["children"] if "sandbox_id" in c]
    if len(kids) < 2:
        pytest.skip("child VMs did not start: " + str(got["children"]))
    a, b = kids[0]["sandbox_id"], kids[1]["sandbox_id"]
    ra = rt.act(a, "exec", {"cmd": "echo A > /tmp/who; cat /tmp/who"})
    rb = rt.act(b, "exec", {"cmd": "cat /tmp/who"})
    assert ra["status"] == "ok" and "A" in ra["observation"]["stdout"]
    assert rb["status"] == "ok" and "parent" in rb["observation"]["stdout"]
    parent = rt.act(sid, "exec", {"cmd": "cat /tmp/who"})
    assert "parent" in parent["observation"]["stdout"]
    for box_id in (sid, a, b):
        vm = rt._box(box_id).vm
        if vm is not None:
            vm.kill()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

"""VM RestoreState check: packed memfile unpack, helper, local harness.

    python3 -m pytest tests/test_trace_vm_replay.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ew_examples import Runtime, TraceError                         # noqa: E402
from ew_examples.fc import FirecrackerConfig, FirecrackerVM          # noqa: E402
from ew_examples.uffd import (                                      # noqa: E402
    DirtyMemfile, capture_dirty_memfile, file_load_body, write_dirty_memfile,
)
from ew_examples.vm_replay import check_restore, diagnose           # noqa: E402

PAGE = 4096
ASSETS = os.environ.get("TRACE_FC_ASSETS", "/tmp/trace-fc-assets")


def test_materialize_dense_unpacks_packed_pages(tmp_path):
    pages = {
        0: b"ZERO" + b"\x00" * (PAGE - 4),
        3: b"THRE" + b"\x00" * (PAGE - 4),
    }
    packed = str(tmp_path / "memfile")
    write_dirty_memfile(packed, pages, page_size=PAGE, n_pages=8)
    mem = DirtyMemfile(packed)
    assert mem.packed is True
    dense = str(tmp_path / "dense.bin")
    mem.materialize_dense(dense)
    blob = open(dense, "rb").read()
    assert len(blob) == 8 * PAGE
    assert blob[:4] == b"ZERO"
    assert blob[3 * PAGE:3 * PAGE + 4] == b"THRE"
    assert blob[PAGE:PAGE + 4] == b"\x00\x00\x00\x00"
    again = DirtyMemfile(dense)
    assert again.packed is False
    assert again.page(0).startswith(b"ZERO")
    assert again.page(3).startswith(b"THRE")


def test_materialize_dense_in_place_and_already_dense(tmp_path):
    full = str(tmp_path / "full.bin")
    blob = bytearray(4 * PAGE)
    blob[0:4] = b"FULL"
    blob[2 * PAGE:2 * PAGE + 4] = b"PAGE"
    open(full, "wb").write(blob)
    packed = str(tmp_path / "memfile")
    capture_dirty_memfile(full, packed)
    mem = DirtyMemfile(packed)
    mem.materialize_dense(packed)
    data = open(packed, "rb").read()
    assert data[:4] == b"FULL"
    assert data[2 * PAGE:2 * PAGE + 4] == b"PAGE"
    dest = str(tmp_path / "copy.bin")
    DirtyMemfile(packed).materialize_dense(dest)
    assert open(dest, "rb").read() == data


def test_file_load_body_uses_file_backend():
    body = file_load_body()
    assert body["mem_backend"]["backend_type"] == "File"
    assert body["mem_backend"]["backend_path"] == "memfile"
    assert body["resume_vm"] is True


class _DummyProc:
    def poll(self):
        return None

    def send_signal(self, _sig):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


def _stub_vm(tmp_path):
    cfg = FirecrackerConfig(bin="/bin/true", kernel="/dev/null", rootfs="/dev/null")
    jail = str(tmp_path / "jail")
    os.makedirs(jail)
    rootfs = os.path.join(jail, "rootfs.ext4")
    open(rootfs, "wb").write(b"disk")
    snap = str(tmp_path / "snapfile")
    open(snap, "wb").write(b"SNAP")
    packed = str(tmp_path / "memfile")
    write_dirty_memfile(packed, {1: b"A" * PAGE}, page_size=PAGE, n_pages=4)
    vm = FirecrackerVM(cfg, jail)
    bodies: list[dict] = []

    def fake_spawn():
        vm.proc = _DummyProc()

    def fake_put(body):
        bodies.append(body)

    vm._spawn = fake_spawn
    vm._put_load = fake_put
    vm._wait_agent = lambda seconds=20.0: None
    return vm, snap, packed, rootfs, bodies


def test_load_snapshot_file_backend_unpacks_packed_memfile(tmp_path):
    vm, snap, packed, rootfs, bodies = _stub_vm(tmp_path)
    vm.load_snapshot(snap, packed, rootfs, use_uffd=False)
    jail_mem = os.path.join(vm.jail, "memfile")
    data = open(jail_mem, "rb").read()
    assert len(data) == 4 * PAGE
    assert data[PAGE:PAGE + 4] == b"AAAA"
    assert data[:4] == b"\x00\x00\x00\x00"
    assert bodies and bodies[0]["mem_backend"]["backend_type"] == "File"


def test_load_snapshot_uffd_failure_retries_file(tmp_path):
    vm, snap, packed, rootfs, bodies = _stub_vm(tmp_path)

    def fake_put(body):
        bodies.append(body)
        if body["mem_backend"]["backend_type"] == "Uffd":
            raise TraceError("VmmFailed", "uffd refused")

    vm._put_load = fake_put
    vm.load_snapshot(snap, packed, rootfs, use_uffd=True)
    kinds = [b["mem_backend"]["backend_type"] for b in bodies]
    assert kinds == ["Uffd", "File"]
    jail_mem = os.path.join(vm.jail, "memfile")
    data = open(jail_mem, "rb").read()
    assert len(data) == 4 * PAGE
    assert data[PAGE:PAGE + 4] == b"AAAA"
    assert vm.uffd is None


def test_diagnose_not_ready_without_assets(tmp_path):
    got = diagnose(str(tmp_path / "missing-assets"))
    assert got["ready"] is False
    assert got["files"]["firecracker"] is False
    assert got["files"]["kernel"] is False
    assert got["files"]["rootfs"] is False
    assert got["notes"]


def test_check_restore_skips_without_assets(tmp_path):
    got = check_restore(str(tmp_path / "root"), str(tmp_path / "no-assets"))
    assert got["result"] == "SKIP"


def test_restore_and_replay_without_span_is_restore_only(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("counter", 3)
    got = rt.restore_and_replay(started["state_id"])
    assert got["replay"] is None
    assert got["backend"] == "episode"
    assert got["sandbox_id"] != started["sandbox_id"]
    assert got["state_id"] == started["state_id"]
    env = rt.act(got["sandbox_id"], "peek", {})
    assert env["status"] == "ok"


def test_restore_and_replay_optional_span(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("counter", 3)
    sid = started["sandbox_id"]
    span_id = rt._box(sid).span_id
    first = rt.act(sid, "peek", {})
    assert first["status"] == "ok"
    got = rt.restore_and_replay(
        started["state_id"], span_id=span_id, stop_before_t=2)
    assert got["replay"] is not None
    assert got["replay"]["events_applied"] == 1
    assert got["sandbox_id"] != sid


def test_restore_keeps_files_and_processes_via_harness(tmp_path):
    """Live VM check. Skips when this host cannot boot Firecracker."""
    info = diagnose(ASSETS)
    if not info["ready"]:
        pytest.skip("; ".join(info["notes"]))
    got = check_restore(str(tmp_path), ASSETS)
    if got["result"] == "SKIP":
        pytest.skip(got["reason"])
    if got["result"] == "FAIL" and str(got.get("reason", "")).startswith("VmmFailed"):
        pytest.skip(got["reason"])
    assert got["result"] == "PASS", json.dumps(got, indent=2)
    assert got["restored_sandbox_id"] != got["parent_sandbox_id"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

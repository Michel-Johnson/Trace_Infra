"""Phase 3: Firecracker CommitState / RestoreState.

    python3 -m pytest tests/test_trace_phase3.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ew_examples import FirecrackerConfig, Runtime, TraceError      # noqa: E402
from ew_examples.fc import agent_exec                               # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_C = os.path.join(ROOT, "ew_examples", "guest_agent.c")
ASSETS = os.environ.get("TRACE_FC_ASSETS", "/tmp/trace-fc-assets")


def _compile_agent(dst: str) -> str:
    subprocess.check_call(["gcc", "-static", "-O2", "-o", dst, AGENT_C])
    return dst


def test_guest_agent_exec_over_unix_socket(tmp_path):
    """The framing used on vsock also works on a Unix socket, so we can test
    it without a microVM."""
    bin_path = str(tmp_path / "guest_agent")
    sock_path = str(tmp_path / "agent.sock")
    _compile_agent(bin_path)
    proc = subprocess.Popen([bin_path, "--unix", sock_path])
    try:
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("agent socket missing")
        got = agent_exec(sock_path, "echo hello; echo err >&2; true")
        assert got["exit"] == 0
        assert "hello" in got["stdout"]
        assert "err" in got["stderr"]
        bg = agent_exec(sock_path, "sleep 30 & echo started")
        assert bg["exit"] == 0
        alive = agent_exec(sock_path, "pgrep sleep >/dev/null && echo yes || echo no")
        assert "yes" in alive["stdout"]
        fail = agent_exec(sock_path, "false")
        assert fail["exit"] != 0
    finally:
        proc.kill()
        proc.wait(timeout=2)


def test_start_run_still_defaults_to_episode(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("verify_solutions", 3)
    assert started["backend"] == "episode"
    env = rt.act(started["sandbox_id"], "list_candidates", {})
    assert env["status"] == "ok"


def test_firecracker_backend_needs_config(tmp_path):
    rt = Runtime(str(tmp_path))
    with pytest.raises(TraceError) as e:
        rt.start_run("shell", backend="firecracker")
    assert e.value.code == "VmmFailed"


def test_firecracker_branch_is_not_phase3(tmp_path):
    rt = Runtime(str(tmp_path))
    box_id = "x"
    from ew_examples.trace import _Box, FC_BACKEND
    rt._boxes[box_id] = _Box(sandbox_id=box_id, run_id="r", backend=FC_BACKEND)
    with pytest.raises(TraceError) as e:
        rt.branch(box_id, 2)
    assert e.value.code == "BranchFailed"
    assert "forkd" in e.value.message


def test_snapshot_dir_layout_helpers(tmp_path):
    rt = Runtime(str(tmp_path))
    sid = "01TESTSTATEID000000000000"
    rt.store.write_metadata(sid, {"backend": "firecracker", "t": 0, "protocol": 1})
    rt.store.write_header(sid, {"vcpu_count": 1, "mem_size_mib": 128})
    d = rt.store.fc_dir(sid)
    assert os.path.isfile(os.path.join(d, "metadata.json"))
    assert os.path.isfile(os.path.join(d, "header"))
    assert d.endswith(os.path.join("snapshots", sid))


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


def test_restore_keeps_files_and_processes(tmp_path):
    """Acceptance: after RestoreState, the file and the process are still there.

    This cloud host currently kills Firecracker at InstanceStart (nested KVM
    oops). The test skips instead of inventing a fake VMM.
    """
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
    wrote = rt.act(sid, "exec", {"cmd": "echo keepme > /tmp/marker; cat /tmp/marker"})
    assert wrote["status"] == "ok"
    assert "keepme" in wrote["observation"]["stdout"]
    bg = rt.act(sid, "exec", {"cmd": "sleep 120 & echo $!"})
    assert bg["status"] == "ok"
    committed = rt.commit_state(sid, prompt={"system": "fc", "transcript": []})
    for name in ("snapfile", "memfile", "upper", "header", "metadata.json"):
        assert os.path.isfile(os.path.join(committed["snapshot_uri"], name))
    restored = rt.restore_state(committed["state_id"])
    assert restored["backend"] == "firecracker"
    assert restored["sandbox_id"] != sid
    marker = rt.act(restored["sandbox_id"], "exec",
                    {"cmd": "cat /tmp/marker"})
    assert marker["status"] == "ok"
    assert "keepme" in marker["observation"]["stdout"]
    alive = rt.act(restored["sandbox_id"], "exec",
                   {"cmd": "pgrep sleep >/dev/null && echo yes || echo no"})
    assert "yes" in alive["observation"]["stdout"]
    rt._box(sid).vm.kill()
    rt._box(restored["sandbox_id"]).vm.kill()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

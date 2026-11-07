"""Reader -> framer -> receiver over REAL OS pipes between SEPARATE processes, fake fixture
trees only. Runs as the real non-root test user (no uid injection). POSIX-only: skipped on
Windows, where the library itself fails closed. Every test is time-bounded."""
import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

if os.name != "posix":
    pytest.skip("POSIX execution only; Windows fails closed (tested elsewhere)", allow_module_level=True)
if os.geteuid() == 0:
    pytest.skip("must run as a real non-root user", allow_module_level=True)

from grok_bot_build_bridge import snapshot_posix as S  # noqa: E402
from grok_bot_build_bridge.snapshot_frames import FrameLimits, encode  # noqa: E402

SRC = str(Path(__file__).resolve().parents[1] / "src")
ENV = {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}



def _fds() -> int:
    return len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else -1


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        st = os.lstat(p)
        h.update(f"{p.relative_to(root)}|{st.st_mode}|{st.st_size}|{st.st_mtime_ns}".encode())
        if p.is_file() and not p.is_symlink():
            h.update(p.read_bytes())
    return h.hexdigest()


def _repo(tmp: Path) -> Path:
    r = tmp / "repo"
    (r / "src" / "pkg").mkdir(parents=True)
    (r / "src" / "pkg" / "a.py").write_text("print('fake')\n")
    (r / "src" / "pkg" / "big.txt").write_bytes(b"fake-data\n" * 2000)
    (r / "src" / "AGENTS.md").write_text("untrusted instructions")
    return r


def _stage(tmp: Path) -> Path:
    s = tmp / "stage"
    s.mkdir(mode=0o700)
    return s


def _child(code: str, **kw) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code], env=ENV, close_fds=True, **kw)


SENDER = """
import os, sys
from grok_bot_build_bridge import snapshot_posix as S
rep = S.read_selection(sys.argv[1], ["src"])
S.send_report(rep, 1, deadline_s=20)
"""


def _sender(repo: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", SENDER, str(repo)], env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)


def test_child_reader_to_parent_receiver_positive_no_source_writes_no_fd_leak(tmp_path):
    repo, stage = _repo(tmp_path), _stage(tmp_path)
    before, fds = _tree_digest(repo), _fds()
    p = _sender(repo)
    res = S.receive_snapshot(p.stdout.fileno(), str(stage), deadline_s=20)
    p.stdout.close()
    assert p.wait(20) == 0, p.stderr.read()
    p.stderr.close()
    assert res.file_count == 2 and _fds() == fds
    snap = Path(res.snapshot_dir)
    assert sorted(os.listdir(snap)) == ["COMPLETE", "files"]
    assert (snap / "files/src/pkg/big.txt").read_bytes() == b"fake-data\n" * 2000
    assert not (snap / "files/src/AGENTS.md").exists()  # instruction file excluded
    for d, _, fs in os.walk(snap):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
        assert all(stat.S_IMODE(os.stat(os.path.join(d, f)).st_mode) == 0o600 for f in fs)
    assert _tree_digest(repo) == before


RECEIVER = """
import json, os, sys
from grok_bot_build_bridge import snapshot_posix as S
from grok_bot_build_bridge.snapshot_frames import FrameLimits
lim = FrameLimits(max_file_bytes=int(sys.argv[2])) if len(sys.argv) > 2 else FrameLimits()
mode = sys.argv[3] if len(sys.argv) > 3 else ""
if mode == "crash":
    S._DiskSink.write = lambda self, chunk: os._exit(9)
if mode == "noread":
    import time; time.sleep(30)
try:
    r = S.receive_snapshot(0, sys.argv[1], limits=lim, deadline_s=20)
    print(json.dumps({"ok": True, "files": r.file_count}))
except S.SnapshotError as e:
    print(json.dumps({"ok": False, "code": e.code})); sys.exit(3)
"""


def _receiver(stage: Path, *extra: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", RECEIVER, str(stage), *extra], env=ENV,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            close_fds=True)


def test_parent_reader_to_child_receiver_positive(tmp_path):
    repo, stage = _repo(tmp_path), _stage(tmp_path)
    rep = S.read_selection(str(repo), ["src"])
    p = _receiver(stage)
    S.send_report(rep, p.stdin.fileno(), deadline_s=20)
    p.stdin.close()
    p.stdin = None
    out, err = p.communicate(timeout=20)
    assert p.returncode == 0 and json.loads(out) == {"ok": True, "files": 2}, err


def test_receiver_rejects_manifest_sender_gets_broken_pipe(tmp_path):
    repo, stage = _repo(tmp_path), _stage(tmp_path)
    (repo / "src" / "huge.bin").write_bytes(b"\0" * 200_000)  # > pipe buffer
    rep = S.read_selection(str(repo), ["src"])
    p = _receiver(stage, "1000")  # child limit rejects manifest
    with pytest.raises(S.SnapshotError) as e:
        S.send_report(rep, p.stdin.fileno(), deadline_s=20)
    assert e.value.code == "peer_closed"
    p.stdin.close()
    p.stdin = None
    out, _ = p.communicate(timeout=20)
    assert p.returncode == 3 and json.loads(out)["ok"] is False and os.listdir(stage) == []


def test_stalled_reader_sender_deadline(tmp_path):
    repo, stage = _repo(tmp_path), _stage(tmp_path)
    (repo / "src" / "huge.bin").write_bytes(b"\0" * 200_000)
    rep = S.read_selection(str(repo), ["src"])
    p = _receiver(stage, "1000000", "noread")
    t0 = time.monotonic()
    try:
        with pytest.raises(S.SnapshotError) as e:
            S.send_report(rep, p.stdin.fileno(), deadline_s=0.5)
        assert e.value.code == "deadline_exceeded" and time.monotonic() - t0 < 5
    finally:
        p.kill()
        p.communicate(timeout=10)


STALL = "import sys,time\nsys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); sys.stdout.flush(); time.sleep(float(sys.argv[2]))"


def _stream(files) -> bytes:
    b = io.BytesIO()
    encode(files, b)
    return b.getvalue()


@pytest.mark.parametrize("prefix_len", [0, 30, None])  # None = whole stream but no EOF
def test_stalled_writer_deadline_no_premature_complete(tmp_path, prefix_len):
    stage = _stage(tmp_path)
    data = _stream([("a.txt", b"hello")])
    chunk = data if prefix_len is None else data[:prefix_len]
    p = subprocess.Popen([sys.executable, "-c", STALL, chunk.hex(), "30"], stdout=subprocess.PIPE, close_fds=True)
    fds, t0 = _fds(), time.monotonic()
    try:
        with pytest.raises(S.SnapshotError) as e:
            S.receive_snapshot(p.stdout.fileno(), str(stage), deadline_s=1.0)
        assert e.value.code == "deadline_exceeded" and time.monotonic() - t0 < 5
        assert os.listdir(stage) == []  # never COMPLETE before trailing-EOF check
    finally:
        p.kill()
        p.wait(10)
        p.stdout.close()
    assert _fds() == fds - 1  # only the pipe we just closed


def test_slow_trickle_writer_succeeds(tmp_path):
    stage = _stage(tmp_path)
    data = _stream([("a.txt", b"hello"), ("d/b.txt", b"x" * 300)])
    code = ("import sys,time,os\nd=bytes.fromhex(sys.argv[1])\n"
            "for i in range(0,len(d),7):\n os.write(1,d[i:i+7]); time.sleep(0.002)")
    p = subprocess.Popen([sys.executable, "-c", code, data.hex()], stdout=subprocess.PIPE, close_fds=True)
    res = S.receive_snapshot(p.stdout.fileno(), str(stage), deadline_s=20)
    p.wait(10)
    p.stdout.close()
    assert res.file_count == 2


def test_cancel_while_writer_stalled(tmp_path):
    stage = _stage(tmp_path)
    p = subprocess.Popen([sys.executable, "-c", STALL, "", "30"], stdout=subprocess.PIPE, close_fds=True)
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    try:
        with pytest.raises(S.SnapshotError) as e:
            S.receive_snapshot(p.stdout.fileno(), str(stage), cancel=ev)
        assert e.value.code == "cancelled" and os.listdir(stage) == []
    finally:
        p.kill()
        p.wait(10)
        p.stdout.close()


@pytest.mark.parametrize("mode", ["close_before_end", "trailing"])
def test_writer_closes_early_or_appends(tmp_path, mode):
    stage = _stage(tmp_path)
    data = _stream([("a.txt", b"hello")])
    data = data[:-12] if mode == "close_before_end" else data + b"X"
    p = subprocess.Popen([sys.executable, "-c", STALL, data.hex(), "0"], stdout=subprocess.PIPE, close_fds=True)
    with pytest.raises(S.SnapshotError) as e:
        S.receive_snapshot(p.stdout.fileno(), str(stage), deadline_s=10)
    p.wait(10)
    p.stdout.close()
    assert e.value.code == ("truncated" if mode == "close_before_end" else "trailing_data")
    assert os.listdir(stage) == []


def test_os_exit_crash_leaves_private_incomplete(tmp_path):
    stage = _stage(tmp_path)
    p = _receiver(stage, "1000000", "crash")
    p.communicate(_stream([("a.txt", b"hello")]), timeout=20)
    assert p.returncode == 9
    (left,) = os.listdir(stage)
    snap = stage / left
    assert left.startswith(S.PREFIX) and stat.S_IMODE(os.stat(snap).st_mode) == 0o700
    assert sorted(os.listdir(snap)) == ["INCOMPLETE", "files"]
    assert stat.S_IMODE(os.stat(snap / "files").st_mode) == 0o700


def test_marker_names_in_manifest_cannot_clash(tmp_path):
    stage = _stage(tmp_path)
    data = _stream([("COMPLETE", b"fake"), ("INCOMPLETE", b"fake"), ("files/x", b"fake")])
    res = S.receive_snapshot(io.BytesIO(data), str(stage))
    snap = Path(res.snapshot_dir)
    assert sorted(os.listdir(snap)) == ["COMPLETE", "files"]
    assert (snap / "COMPLETE").read_bytes() == b""  # marker untouched by payload
    assert sorted(os.listdir(snap / "files")) == ["COMPLETE", "INCOMPLETE", "files"]


def test_cleanup_with_symlinked_parent_does_not_touch_victim(tmp_path):
    stage = _stage(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()

    def plant(name):
        os.symlink(victim, stage / name / "files" / "d")
    with pytest.raises((S.SnapshotError, OSError)):
        S.receive_snapshot(io.BytesIO(_stream([("d/b.txt", b"fake")])), str(stage), _after_create=plant)
    assert os.listdir(victim) == [] and os.listdir(stage) == []


def test_group_world_writable_source_dirs_refused(tmp_path):
    repo = _repo(tmp_path)
    os.chmod(repo / "src" / "pkg", 0o777)
    rep = S.read_selection(str(repo), ["src"])
    assert rep.skipped.get("untrusted_dir") == 1 and rep.files == []
    os.chmod(repo, 0o777)
    with pytest.raises(S.SnapshotError) as e:
        S.read_selection(str(repo), ["src"])
    assert e.value.code == "untrusted_dir"


def test_deadline_requires_raw_fd(tmp_path):
    with pytest.raises(S.SnapshotError) as e:
        S.receive_snapshot(io.BytesIO(b""), str(_stage(tmp_path)), deadline_s=1)
    assert e.value.code == "deadline_needs_fd"

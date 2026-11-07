"""POSIX snapshot reader/receiver on FAKE fixture trees only. Linux-positive; Windows is
asserted to fail closed. uid_provider is injected ONLY so root CI hosts can exercise the
code; production default is os.geteuid and root is refused."""
import io
import os
import stat
import sys
import threading

import pytest

from grok_bot_build_bridge import snapshot_posix as S
from grok_bot_build_bridge.snapshot_frames import FrameLimits, encode

posix = pytest.mark.skipif(os.name != "posix", reason="POSIX-only library")
USER = lambda: 4242 if os.geteuid() == 0 else os.geteuid()  # noqa: E731


def test_windows_or_missing_primitives_fail_closed(monkeypatch):
    monkeypatch.setattr(S.os, "name", "nt")
    with pytest.raises(S.SnapshotError) as e:
        S.read_selection("/x", ["a"], uid_provider=USER)
    assert e.value.code == "platform_unsupported"


@posix
def test_root_refused_by_default_policy(tmp_path):
    for fn in (lambda: S.read_selection(str(tmp_path), ["a"], uid_provider=lambda: 0),
               lambda: S.receive_snapshot(io.BytesIO(b""), str(tmp_path), uid_provider=lambda: 0)):
        with pytest.raises(S.SnapshotError) as e:
            fn()
        assert e.value.code == "root_refused"


def _repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src" / "pkg").mkdir(parents=True)
    (r / "src" / "pkg" / "a.py").write_text("print('fake')\n")
    (r / "src" / "pkg" / "b.txt").write_text("dummy\n")
    (r / "src" / ".env").write_text("FAKE=1\n")
    (r / "src" / "AGENTS.md").write_text("fake instructions")
    (r / "src" / "node_modules").mkdir()
    (r / "src" / "node_modules" / "x.js").write_text("1")
    return r


@posix
def test_reader_positive_and_privacy_summary(tmp_path):
    r = _repo(tmp_path)
    rep = S.read_selection(str(r), ["src"], uid_provider=USER)
    assert [p for p, _ in rep.files] == ["src/pkg/a.py", "src/pkg/b.txt"]
    sm = rep.summary()
    assert sm["skipped_by_reason"] == {"excluded_dir": 1, "instruction_file_excluded": 1, "secret_like": 1}
    assert "a.py" not in str(sm) and "fake" not in str(sm)


@posix
def test_reader_rejects_symlinks_fifo_hardlink_parent_and_root(tmp_path):
    r = _repo(tmp_path)
    out = tmp_path / "outside"
    out.mkdir()
    (out / "s.txt").write_text("outside")
    os.symlink(out / "s.txt", r / "src" / "pkg" / "lnk.py")
    os.symlink(out, r / "src" / "dirlink")
    os.mkfifo(r / "src" / "pkg" / "fifo")
    os.link(r / "src" / "pkg" / "b.txt", r / "src" / "pkg" / "b_alias.txt")
    rep = S.read_selection(str(r), ["src", "dirlink/s.txt"], uid_provider=USER)
    assert [p for p, _ in rep.files] == ["src/pkg/a.py"]
    sk = rep.skipped
    # dir symlink seen during the walk is skipped as a symlink entry (never opened)
    assert sk["symlink"] == 2 and sk["special_file"] == 1 and sk["hardlink"] == 2
    assert sk["missing"] == 1 and "symlink_dir" not in sk
    os.symlink(r, tmp_path / "rootlink")
    with pytest.raises(S.SnapshotError) as e:
        S.read_selection(str(tmp_path / "rootlink"), ["src"], uid_provider=USER)
    assert e.value.code == "symlink_or_not_dir"


@posix
@pytest.mark.parametrize("stage,action,reason", [
    ("pre_open", "symlink", "symlink"),
    ("pre_open", "fifo", "changed_before_read"),
    ("pre_open", "replace", "changed_before_read"),
    ("pre_read", "grow", "changed_during_read"),
    ("pre_read", "shrink", "changed_during_read"),
    ("post_read", "touch", "changed_during_read"),
    ("pre_open", "hardlink", "changed_before_read"),
])
def test_swaps_at_each_boundary(tmp_path, stage, action, reason):
    r = _repo(tmp_path)
    target = r / "src" / "pkg" / "a.py"
    outside = tmp_path / "secret.txt"
    outside.write_text("outside secret")

    def hook(st, rel):
        if st != stage or rel != "src/pkg/a.py":
            return
        if action == "symlink":
            target.unlink(); os.symlink(outside, target)
        elif action == "fifo":
            target.unlink(); os.mkfifo(target)
        elif action == "replace":
            os.rename(target, tmp_path / "old_a"); target.write_text("print('fake')\n")
        elif action == "grow":
            with open(target, "a") as f:
                f.write("more")
        elif action == "shrink":
            with open(target, "w") as f:
                f.write("x")
        elif action == "touch":
            os.utime(target, ns=(1, 1))
        elif action == "hardlink":
            os.link(target, tmp_path / "alias")
    rep = S.read_selection(str(r), ["src/pkg/a.py"], uid_provider=USER, _hook=hook)
    assert rep.files == [] and rep.skipped == {reason: 1}


@posix
def test_parent_dir_swapped_for_symlink(tmp_path):
    r = _repo(tmp_path)
    out = tmp_path / "out"
    (out / "pkg").mkdir(parents=True)
    (out / "pkg" / "a.py").write_text("outside")

    def hook(st, rel):
        if st == "pre_parent_open":
            os.rename(r / "src", tmp_path / "moved")
            os.symlink(out, r / "src")
    rep = S.read_selection(str(r), ["src/pkg/a.py"], uid_provider=USER, _hook=hook)
    assert rep.files == [] and rep.skipped == {"symlink_dir": 1}


@posix
def test_limits_before_read(tmp_path):
    r = tmp_path / "r"
    (r / "d").mkdir(parents=True)
    for i in range(5):
        (r / "d" / f"f{i}").write_bytes(b"x" * 100)
    (r / "d" / "big").write_bytes(b"x" * 5000)
    rep = S.read_selection(str(r), ["d"], uid_provider=USER,
                           limits=S.ReadLimits(max_files=3, max_file_bytes=1000))
    assert len(rep.files) == 3 and rep.skipped["file_too_large"] == 1 and rep.skipped["over_limit"] == 2


# ------------------------------ receiver ------------------------------

def _stream(files):
    b = io.BytesIO()
    encode(files, b)
    b.seek(0)
    return b


@posix
def test_end_to_end_reader_pipe_receiver(tmp_path):
    r = _repo(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    rep = S.read_selection(str(r), ["src"], uid_provider=USER)
    rfd, wfd = os.pipe()
    res = {}

    def recv():
        with os.fdopen(rfd, "rb") as rf:
            res["r"] = S.receive_snapshot(rf, str(stage), uid_provider=USER)
    t = threading.Thread(target=recv)
    t.start()
    with os.fdopen(wfd, "wb") as wf:
        S.encode_report(rep, wf)
    t.join(10)
    out = res["r"]
    assert out.file_count == 2 and out.total_bytes == sum(len(b) for _, b in rep.files)
    snap = os.path.dirname(out.files_dir)
    assert sorted(os.listdir(snap)) == ["COMPLETE", "files"]
    assert stat.S_IMODE(os.stat(snap).st_mode) == 0o700
    f = os.path.join(out.files_dir, "src", "pkg", "a.py")
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600 and open(f).read() == "print('fake')\n"
    assert stat.S_IMODE(os.stat(os.path.join(out.files_dir, "src")).st_mode) == 0o700


@posix
def test_receiver_corruption_cleans_only_own_dir(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    (stage / "unrelated").write_text("keep me")
    data = bytearray(_stream([("a.txt", b"hello"), ("d/b.txt", b"world")]).getvalue())
    data[data.index(b"world")] ^= 1
    with pytest.raises(S.SnapshotError) as e:
        S.receive_snapshot(io.BytesIO(bytes(data)), str(stage), uid_provider=USER)
    assert e.value.code == "file_hash_mismatch"
    assert os.listdir(stage) == ["unrelated"] and (stage / "unrelated").read_text() == "keep me"


@posix
@pytest.mark.parametrize("cut", [20, 60, -10])
def test_receiver_truncation_no_partial_commit(tmp_path, cut):
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    data = _stream([("a.txt", b"hello"), ("b.txt", b"x" * 1000)]).getvalue()
    with pytest.raises(S.SnapshotError):
        S.receive_snapshot(io.BytesIO(data[:cut]), str(stage), uid_provider=USER)
    assert os.listdir(stage) == []


@posix
def test_cleanup_does_not_follow_planted_symlink(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("do not delete")

    def plant(name):
        os.symlink(victim, stage / name / "files" / "evil")
    data = bytearray(_stream([("a.txt", b"hello")]).getvalue())
    data[-1] ^= 1
    with pytest.raises(S.SnapshotError):
        S.receive_snapshot(io.BytesIO(bytes(data)), str(stage), uid_provider=USER, _after_create=plant)
    assert (victim / "precious.txt").read_text() == "do not delete" and os.listdir(stage) == []


@posix
def test_interrupted_receiver_leaves_private_incomplete(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    monkeypatch.setattr(S, "_rmtree_fd", lambda fd: (_ for _ in ()).throw(OSError("simulated crash")))

    class Boom(io.BytesIO):
        def read(self, n=-1):
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        S.receive_snapshot(Boom(), str(stage), uid_provider=USER)
    left = os.listdir(stage)
    assert len(left) == 1 and left[0].startswith(S.PREFIX)
    snap = stage / left[0]
    assert stat.S_IMODE(os.stat(snap).st_mode) == 0o700
    assert "INCOMPLETE" in os.listdir(snap) and "COMPLETE" not in os.listdir(snap)


@posix
def test_untrusted_staging_parent_refused(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o777)
    with pytest.raises(S.SnapshotError) as e:
        S.receive_snapshot(_stream([("a", b"x")]), str(loose), uid_provider=USER)
    assert e.value.code in {"staging_parent_untrusted"}
    os.symlink(tmp_path, tmp_path / "linkparent")
    with pytest.raises(S.SnapshotError) as e:
        S.receive_snapshot(_stream([("a", b"x")]), str(tmp_path / "linkparent"), uid_provider=USER)
    assert e.value.code == "symlink_or_not_dir"
    assert os.listdir(loose) == []


@posix
def test_no_archives_and_bounded(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    with pytest.raises(S.SnapshotError) as e:
        S.receive_snapshot(io.BytesIO(b"PK\x03\x04" + b"\0" * 40), str(stage), uid_provider=USER)
    assert e.value.code == "bad_magic" and os.listdir(stage) == []
    with pytest.raises(S.SnapshotError) as e:
        S.receive_snapshot(_stream([("a", b"x" * 10)]), str(stage), uid_provider=USER,
                           limits=FrameLimits(max_file_bytes=5))
    assert e.value.code == "size_invalid"

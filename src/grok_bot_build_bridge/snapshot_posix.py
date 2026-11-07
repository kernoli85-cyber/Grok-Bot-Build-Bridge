"""POSIX (WSL-side) snapshot reader + receiver library. NO CLI, not wired to any command.

Reader: walks an owner-selected root with dir fds only (O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC per
component, starting from "/"), never resolving multi-component paths. Files are pre-checked
with fstatat(no-follow), opened O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC, verified by fstat
(regular, st_nlink == 1, same st_dev as root, same inode as the pre-check, size bound),
read to exactly the fstat size (+1 probe byte), then fstat'ed again: inode, dev, size,
mtime_ns and ctime_ns must be unchanged.

Receiver: creates a fresh random 0700 dir under a trusted staging parent (root-owned sticky
dir like /tmp, or a 0700 dir owned by the caller), writes files fd-relative with
O_CREAT|O_EXCL|O_NOFOLLOW (0600) under files/, and marks the snapshot COMPLETE only after the
gbbb.snapshot/v1 stream (snapshot_frames) has fully verified. On failure it removes only its
own random dir, fd-relative, never following symlinks.

Honest limits (see docs/SNAPSHOT_TRANSFER.md):
- In-place content changes between the two fstats are not detectable in general; a process
  that restores timestamps can defeat the stat comparison. Not an atomic snapshot.
- A hostile process running as the SAME user is out of scope (it can do anything we can).
- SIGKILL/crash cannot be cleaned up; leftovers stay private (0700) and marked INCOMPLETE.
- Running as root is refused (identity is injectable ONLY for tests on root CI hosts).
- Windows / non-POSIX: refused (platform_unsupported); no lstat-based fallback.
"""
from __future__ import annotations

import errno
import io
import os
import secrets
import stat
from dataclasses import dataclass
from typing import BinaryIO, Callable

from .repo_plan import (EXCLUDED_DIRS, INSTRUCTION_NAMES, PlanRefused, is_secret_name,
                        normalize_selection)
from .snapshot_frames import FileSink, FrameError, FrameLimits, decode, encode

DIR_FLAGS = getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | os.O_RDONLY
MARK_INCOMPLETE = "INCOMPLETE"
MARK_COMPLETE = "COMPLETE"
FILES_DIR = "files"
PREFIX = "gbbb-repo-"


class SnapshotError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def require_platform() -> None:
    needed = ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK")
    if os.name != "posix" or not all(hasattr(os, n) for n in needed) \
            or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd \
            or os.mkdir not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd:
        raise SnapshotError("platform_unsupported", "secure dir-fd file access unavailable on this platform")


def _refuse_root(uid_provider: Callable[[], int]) -> int:
    uid = uid_provider()
    if uid == 0:
        raise SnapshotError("root_refused", "snapshot reader/receiver must not run as root")
    return uid


def _open_dir_chain(path: str) -> int:
    """Open an absolute directory path component by component from '/', no symlinks."""
    if not path.startswith("/") or "\x00" in path:
        raise SnapshotError("path_not_absolute", "absolute POSIX path required")
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise SnapshotError("path_traversal", "'.'/'..' components refused")
    fd = os.open("/", DIR_FLAGS)
    try:
        for comp in parts:
            try:
                nfd = os.open(comp, DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise SnapshotError("symlink_or_not_dir", "path component is a symlink or not a directory") from None
                raise SnapshotError("open_failed", "cannot open path component") from None
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


# ------------------------------- reader ---------------------------------

@dataclass(frozen=True)
class ReadLimits:
    max_files: int = 200
    max_file_bytes: int = 256 * 1024
    max_total_bytes: int = 2 * 1024 * 1024
    max_depth: int = 8
    max_entries_scanned: int = 5000


@dataclass
class ReadReport:
    files: list[tuple[str, bytes]]
    skipped: dict[str, int]

    def summary(self) -> dict:
        """Privacy-safe: counts only, no names, contents or hashes."""
        return {"files": len(self.files), "bytes": sum(len(b) for _, b in self.files),
                "skipped_by_reason": dict(sorted(self.skipped.items()))}


_BeforeRead = Callable[[str, str], None]  # test hook: (stage, relpath)


def read_selection(root: str, selections: list[str], *, limits: ReadLimits = ReadLimits(),
                   include_instruction_files: bool = False,
                   uid_provider: Callable[[], int] = os.geteuid if hasattr(os, "geteuid") else (lambda: -1),
                   _hook: _BeforeRead | None = None) -> ReadReport:
    require_platform()
    _refuse_root(uid_provider)
    if not selections:
        raise SnapshotError("selection_required", "explicit selections required")
    try:
        norm = [normalize_selection(s) for s in selections]
    except PlanRefused as exc:
        raise SnapshotError(exc.code, exc.message) from None
    root_fd = _open_dir_chain(root)
    hook = _hook or (lambda s, p: None)
    out: dict[str, bytes] = {}
    skipped: dict[str, int] = {}
    total = 0
    scanned = 0
    try:
        root_dev = os.fstat(root_fd).st_dev

        def skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        def read_file(dfd: int, name: str, rel: str) -> None:
            nonlocal total
            low = name.lower()
            if is_secret_name(name):
                return skip("secret_like")
            if low in INSTRUCTION_NAMES and not include_instruction_files:
                return skip("instruction_file_excluded")
            if low in (".git", ".gitmodules"):
                return skip("git_metadata")
            hook("pre_lstat", rel)
            try:
                pre = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                return skip("vanished")
            if stat.S_ISLNK(pre.st_mode):
                return skip("symlink")
            if not stat.S_ISREG(pre.st_mode):
                return skip("special_file")
            if pre.st_dev != root_dev:
                return skip("other_filesystem")
            if pre.st_nlink != 1:
                return skip("hardlink")
            if pre.st_size > limits.max_file_bytes:
                return skip("file_too_large")
            if len(out) >= limits.max_files or total + pre.st_size > limits.max_total_bytes:
                return skip("over_limit")
            hook("pre_open", rel)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
            except OSError as exc:
                return skip("symlink" if exc.errno == errno.ELOOP else "open_failed")
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_dev != root_dev \
                        or st.st_ino != pre.st_ino or st.st_size > limits.max_file_bytes:
                    return skip("changed_before_read")
                if total + st.st_size > limits.max_total_bytes:
                    return skip("over_limit")
                hook("pre_read", rel)
                buf = bytearray()
                want = st.st_size + 1  # one probe byte detects growth
                while len(buf) < want:
                    chunk = os.read(fd, min(64 * 1024, want - len(buf)))
                    if not chunk:
                        break
                    buf += chunk
                hook("post_read", rel)
                st2 = os.fstat(fd)
                if len(buf) != st.st_size or (st2.st_ino, st2.st_dev, st2.st_size, st2.st_mtime_ns,
                                              st2.st_ctime_ns, st2.st_nlink) != \
                        (st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns, st.st_ctime_ns, 1):
                    return skip("changed_during_read")
            finally:
                os.close(fd)
            if rel.casefold() in {k.casefold() for k in out}:
                return
            out[rel] = bytes(buf)
            total += len(buf)

        def walk(dfd: int, rel: str, depth: int) -> None:
            nonlocal scanned
            names = sorted(os.listdir(dfd))
            if ".git" in names:
                return skip("nested_repo_or_submodule")
            for name in names:
                scanned += 1
                if scanned > limits.max_entries_scanned:
                    return skip("max_entries_scanned")
                crel = f"{rel}/{name}" if rel else name
                try:
                    pre = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except OSError:
                    skip("vanished")
                    continue
                if stat.S_ISDIR(pre.st_mode):
                    if name.lower() in EXCLUDED_DIRS:
                        skip("excluded_dir")
                    elif depth + 1 > limits.max_depth:
                        skip("max_depth")
                    else:
                        sub = open_child_dir(dfd, name)
                        if sub is not None:
                            try:
                                walk(sub, crel, depth + 1)
                            finally:
                                os.close(sub)
                else:
                    read_file(dfd, name, crel)

        def open_child_dir(dfd: int, name: str) -> int | None:
            try:
                fd = os.open(name, DIR_FLAGS, dir_fd=dfd)
            except OSError as exc:
                skip("symlink_dir" if exc.errno in (errno.ELOOP, errno.ENOTDIR)
                     else "missing" if exc.errno == errno.ENOENT else "open_failed")
                return None
            if os.fstat(fd).st_dev != root_dev:
                os.close(fd)
                skip("other_filesystem")
                return None
            return fd

        for sel in norm:
            comps = sel.split("/")
            dfd = os.dup(root_fd)
            ok = True
            try:
                for comp in comps[:-1]:
                    hook("pre_parent_open", sel)
                    nfd = open_child_dir(dfd, comp)
                    if nfd is None:
                        ok = False
                        break
                    os.close(dfd)
                    dfd = nfd
                if not ok:
                    continue
                last = comps[-1]
                try:
                    pre = os.stat(last, dir_fd=dfd, follow_symlinks=False)
                except OSError:
                    skip("missing")
                    continue
                if stat.S_ISDIR(pre.st_mode):
                    sub = open_child_dir(dfd, last)
                    if sub is not None:
                        try:
                            walk(sub, sel, 1)
                        finally:
                            os.close(sub)
                else:
                    read_file(dfd, last, sel)
            finally:
                os.close(dfd)
    finally:
        os.close(root_fd)
    return ReadReport(files=sorted(out.items()), skipped=skipped)


def encode_report(report: ReadReport, out: BinaryIO, *, limits: FrameLimits = FrameLimits()) -> None:
    if not report.files:
        raise SnapshotError("nothing_to_send", "no eligible files")
    encode(report.files, out, limits=limits)


# ------------------------------- receiver -------------------------------

@dataclass(frozen=True)
class ReceiveResult:
    snapshot_dir: str
    files_dir: str
    file_count: int
    total_bytes: int


def _verify_staging_parent(fd: int, uid: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        raise SnapshotError("staging_parent_invalid", "staging parent is not a directory")
    sticky_root = st.st_uid == 0 and (st.st_mode & stat.S_ISVTX)
    private_own = st.st_uid == uid and (st.st_mode & 0o077) == 0
    if not (sticky_root or private_own):
        raise SnapshotError("staging_parent_untrusted",
                            "staging parent must be root-owned sticky (like /tmp) or a 0700 dir owned by the user")


def _rmtree_fd(dfd: int) -> None:
    """Remove all entries below an open dir fd without following symlinks."""
    for name in os.listdir(dfd):
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            sub = os.open(name, DIR_FLAGS, dir_fd=dfd)
            try:
                _rmtree_fd(sub)
            finally:
                os.close(sub)
            os.rmdir(name, dir_fd=dfd)
        else:
            os.unlink(name, dir_fd=dfd)  # removes a symlink itself, never its target


class _DiskSink(FileSink):
    def __init__(self, files_fd: int, path: str, uid: int) -> None:
        self.dirs: list[int] = []
        parent = os.dup(files_fd)
        try:
            for comp in path.split("/")[:-1]:
                try:
                    os.mkdir(comp, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                nfd = os.open(comp, DIR_FLAGS, dir_fd=parent)
                st = os.fstat(nfd)
                if st.st_uid != uid:
                    os.close(nfd)
                    raise FrameError("receiver_dir_not_owned", "unexpected directory owner")
                os.fchmod(nfd, 0o700)
                os.close(parent)
                parent = nfd
            self.name = path.split("/")[-1]
            self.fd = os.open(self.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600, dir_fd=parent)
            os.fchmod(self.fd, 0o600)
            self.parent = parent
        except FileExistsError:
            os.close(parent)
            raise FrameError("receiver_exists", "target already exists") from None
        except BaseException:
            os.close(parent)
            raise

    def write(self, chunk: bytes) -> None:
        view = memoryview(chunk)
        while view:
            n = os.write(self.fd, view)
            view = view[n:]

    def _close(self) -> None:
        for fd in (getattr(self, "fd", None), getattr(self, "parent", None)):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fd = self.parent = None

    def commit(self) -> None:
        os.fsync(self.fd)
        self._close()

    def abort(self) -> None:
        self._close()


def receive_snapshot(stream: BinaryIO, staging_parent: str = "/tmp", *,
                     limits: FrameLimits = FrameLimits(),
                     uid_provider: Callable[[], int] = os.geteuid if hasattr(os, "geteuid") else (lambda: -1),
                     _after_create: Callable[[str], None] | None = None) -> ReceiveResult:
    """Decode a gbbb.snapshot/v1 stream into a fresh private dir. All-or-nothing."""
    require_platform()
    uid = _refuse_root(uid_provider)
    parent_fd = _open_dir_chain(staging_parent)
    name = PREFIX + secrets.token_hex(8)
    created = False
    snap_fd = files_fd = None
    try:
        _verify_staging_parent(parent_fd, uid)
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
        snap_fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
        st = os.fstat(snap_fd)
        if st.st_uid != uid:
            raise SnapshotError("snapshot_dir_not_owned", "fresh dir has unexpected owner")
        os.fchmod(snap_fd, 0o700)
        m = os.open(MARK_INCOMPLETE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600, dir_fd=snap_fd)
        os.close(m)
        os.mkdir(FILES_DIR, 0o700, dir_fd=snap_fd)
        files_fd = os.open(FILES_DIR, DIR_FLAGS, dir_fd=snap_fd)
        if _after_create:
            _after_create(name)
        sinks: list[_DiskSink] = []

        def factory(path: str, size: int) -> FileSink:
            s = _DiskSink(files_fd, path, uid)
            sinks.append(s)
            return s
        try:
            decode(stream, limits=limits, sink_factory=factory)
        except FrameError as exc:
            for s in sinks:
                s.abort()
            raise SnapshotError(exc.code, exc.message) from None
        except BaseException:
            for s in sinks:
                s.abort()
            raise
        m = os.open(MARK_COMPLETE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600, dir_fd=snap_fd)
        os.close(m)
        os.unlink(MARK_INCOMPLETE, dir_fd=snap_fd)
        os.fsync(snap_fd)
        snap_path = staging_parent.rstrip("/") + "/" + name
        return ReceiveResult(snapshot_dir=snap_path, files_dir=snap_path + "/" + FILES_DIR,
                             file_count=len(sinks), total_bytes=_count_bytes(files_fd))
    except BaseException:
        if created:
            try:
                if snap_fd is None:
                    snap_fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
                _rmtree_fd(snap_fd)
                os.close(snap_fd)
                snap_fd = None
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass  # leftover stays private 0700 and marked INCOMPLETE
        raise
    finally:
        for fd in (files_fd, snap_fd, parent_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _count_bytes(dfd: int) -> int:
    total = 0
    for name in os.listdir(dfd):
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            sub = os.open(name, DIR_FLAGS, dir_fd=dfd)
            try:
                total += _count_bytes(sub)
            finally:
                os.close(sub)
        elif stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total

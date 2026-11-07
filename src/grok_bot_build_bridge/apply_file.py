"""Replace one existing POSIX file with reviewed bytes. No model call and no network.

The caller confirms a token bound to the source path, directory identity, file
identity, mode, base bytes, and replacement bytes. This is not a transaction:

- There is no backup and no rollback.
- rename of one name in the held parent directory is the only write of the target.
- A same-user swap in the gap between the last stat and rename cannot be closed.
- A killed process can leave ``.gbbb-apply-<hex>.tmp`` beside the file. This
  process unlinks only the temp it still owns. It does not clean older temps.
- Directory fsync is attempted after rename. If the platform refuses it, that
  is reported and is not claimed as power-loss safety.
"""
from __future__ import annotations

import difflib
import errno
import hashlib
import hmac
import json
import os
import secrets
import stat
from dataclasses import dataclass
from typing import Any, Callable

from .context_pack import _BAD
from .proposal import validate_replacement_object
from .proposal import ProposalError
from .repo_plan import normalize_selection
from .repo_plan import PlanRefused
from .snapshot_posix import DIR_FLAGS
from .snapshot_posix import SnapshotError
from .snapshot_posix import _check_dir_trust
from .snapshot_posix import _refuse_root
from .snapshot_posix import require_platform

_HOOK = Callable[[str], None]
_LIMITS = {
    "atomic": "one directory entry on the same filesystem",
    "rollback": "none",
    "backup": False,
    "race": "same-user swap between the last stat and rename is not closed",
    "crash_leftover": "a killed process may leave .gbbb-apply-*.tmp in the parent directory",
}


class ApplyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class _Opened:
    source: str
    file: str
    data: bytes
    text: str
    dev: int
    ino: int
    mode: int
    uid: int
    parents: tuple[tuple[int, int], ...]


def apply_token(key: bytes, opened: _Opened, replacement: str, *, show_diff: bool,
                lineage: str, proposal_token: str = "", replacement_format: str = "") -> str:
    body = {
        "schema": "gbbb.apply_bind/v1",
        "op": "apply",
        "lineage": lineage,
        "proposal_token": proposal_token,
        "replacement_format": replacement_format,
        "source": opened.source,
        "file": opened.file,
        "dev": opened.dev,
        "ino": opened.ino,
        "mode": opened.mode,
        "uid": opened.uid,
        "parents": [[dev, ino] for dev, ino in opened.parents],
        "base_sha256": hashlib.sha256(opened.data).hexdigest(),
        "replacement_sha256": hashlib.sha256(replacement.encode()).hexdigest(),
        "show_diff": bool(show_diff),
    }
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, b"apply\x00" + payload, hashlib.sha256).hexdigest()[:32]


def _source_parts(source: str) -> list[str]:
    if not isinstance(source, str) or not source.startswith("/") or "\x00" in source or "//" in source:
        raise ApplyError("path_not_absolute", "source must be one absolute POSIX path")
    if source != "/" and source.endswith("/"):
        raise ApplyError("path_not_absolute", "source must be one absolute POSIX path")
    parts = source.split("/")[1:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ApplyError("path_traversal", "source must not contain '.' or '..'")
    return parts


def _open_chain(parts: list[str], uid: int) -> tuple[int, tuple[tuple[int, int], ...]]:
    fd = os.open("/", DIR_FLAGS)
    idents: list[tuple[int, int]] = []
    try:
        for index, comp in enumerate(parts):
            try:
                nfd = os.open(comp, DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                    raise ApplyError("symlink_or_not_dir", "a path component is missing, not a directory, or a symlink") from None
                raise ApplyError("open_failed", "cannot open a path component") from None
            os.close(fd)
            fd = nfd
            st = os.fstat(fd)
            if index == len(parts) - 1:
                try:
                    _check_dir_trust(st, uid)
                except SnapshotError as exc:
                    raise ApplyError(exc.code, exc.message) from None
            idents.append((st.st_dev, st.st_ino))
    except BaseException:
        os.close(fd)
        raise
    return fd, tuple(idents)


def _read_file(parent: int, name: str, root_dev: int, uid: int) -> tuple[bytes, int, int, int, int]:
    try:
        pre = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise ApplyError("missing", "selected file does not exist") from None
        raise ApplyError("open_failed", "selected file cannot be inspected") from None
    if stat.S_ISLNK(pre.st_mode):
        raise ApplyError("symlink", "symlinks are refused")
    if not stat.S_ISREG(pre.st_mode):
        raise ApplyError("special_file", "only a regular file can be replaced")
    if pre.st_dev != root_dev:
        raise ApplyError("other_filesystem", "file is on a different filesystem from the source root")
    if pre.st_nlink != 1:
        raise ApplyError("hardlink", "hardlinked files are refused")
    if pre.st_uid != uid:
        raise ApplyError("not_owner", "file must be owned by the caller")
    perm = stat.S_IMODE(pre.st_mode)
    if perm & 0o7000:
        raise ApplyError("special_mode", "setuid, setgid, and sticky files are refused")
    if perm & 0o022:
        raise ApplyError("group_writable", "group- or world-writable files are refused")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ApplyError("symlink", "symlinks are refused") from None
        raise ApplyError("open_failed", "selected file cannot be opened") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_dev != root_dev \
                or st.st_ino != pre.st_ino or st.st_uid != uid or stat.S_IMODE(st.st_mode) != perm:
            raise ApplyError("changed_before_read", "file changed before it could be read")
        buf = bytearray()
        want = st.st_size + 1
        while len(buf) < want:
            chunk = os.read(fd, min(65536, want - len(buf)))
            if not chunk:
                break
            buf += chunk
        st2 = os.fstat(fd)
        same = (st2.st_ino, st2.st_dev, st2.st_size, st2.st_mtime_ns, st2.st_ctime_ns, st2.st_nlink, stat.S_IMODE(st2.st_mode))
        if len(buf) != st.st_size or same != (st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns, st.st_ctime_ns, 1, perm):
            raise ApplyError("changed_during_read", "file changed while it was read")
    finally:
        os.close(fd)
    return bytes(buf), st.st_dev, st.st_ino, perm, st.st_uid


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ApplyError("base_not_utf8", "file is not strict UTF-8 text") from None
    if "\r" in text or _BAD.search(text):
        raise ApplyError("base_not_utf8", "file is not plain UTF-8 text")
    return text


def _open_target(source: str, file: str) -> _Opened:
    require_platform()
    uid = _refuse_root()
    parts = _source_parts(source)
    try:
        rel = normalize_selection(file)
    except PlanRefused as exc:
        raise ApplyError(exc.code, exc.message) from None
    root, root_idents = _open_chain(parts, uid)
    try:
        root_dev = os.fstat(root).st_dev
        parent = os.dup(root)
        parents = list(root_idents)
        try:
            comps = rel.split("/")
            for comp in comps[:-1]:
                try:
                    nfd = os.open(comp, DIR_FLAGS, dir_fd=parent)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                        raise ApplyError("symlink_or_not_dir", "a parent is missing, not a directory, or a symlink") from None
                    raise ApplyError("open_failed", "cannot open a parent directory") from None
                os.close(parent)
                parent = nfd
                st = os.fstat(parent)
                if st.st_dev != root_dev:
                    raise ApplyError("other_filesystem", "parent is on a different filesystem")
                try:
                    _check_dir_trust(st, uid)
                except SnapshotError as exc:
                    raise ApplyError(exc.code, exc.message) from None
                parents.append((st.st_dev, st.st_ino))
            data, dev, ino, mode, owner = _read_file(parent, comps[-1], root_dev, uid)
        finally:
            os.close(parent)
    finally:
        os.close(root)
    return _Opened(source, rel, data, _decode(data), dev, ino, mode, owner, tuple(parents))


def _diff(path: str, base: str, replacement: str) -> str:
    return "".join(difflib.unified_diff(
        base.splitlines(keepends=True), replacement.splitlines(keepends=True),
        fromfile=path, tofile=path))


def _common(opened: _Opened, replacement: str, *, show_diff: bool, key: bytes,
            lineage: str, proposal_token: str, replacement_format: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": "gbbb.invoke/v1",
        "op": "apply",
        "model_call": False,
        "network": False,
        "retry": False,
        "stored": False,
        "db": False,
        "outbox": False,
        "mcp": False,
        "proposal_saved": False,
        "file": opened.file,
        "lineage": lineage,
        "base_sha256": hashlib.sha256(opened.data).hexdigest(),
        "changed": opened.text != replacement,
        "limits": dict(_LIMITS),
    }
    if show_diff:
        body["diff"] = _diff(opened.file, opened.text, replacement)
    body["apply_token"] = apply_token(
        key, opened, replacement, show_diff=show_diff, lineage=lineage,
        proposal_token=proposal_token, replacement_format=replacement_format)
    return body


def _replace(opened: _Opened, replacement: str, hook: _HOOK | None) -> dict[str, Any]:
    payload = replacement.encode()
    uid = _refuse_root()
    parts = _source_parts(opened.source)
    root, root_idents = _open_chain(parts, uid)
    renamed = False
    temp_name = ""
    parent = -1
    temp_fd = -1
    try:
        if root_idents != opened.parents[:len(root_idents)] or os.fstat(root).st_dev != opened.dev:
            raise ApplyError("directory_changed", "a directory in the source path changed")
        parent = os.dup(root)
        parents = list(root_idents)
        comps = opened.file.split("/")
        for comp in comps[:-1]:
            try:
                nfd = os.open(comp, DIR_FLAGS, dir_fd=parent)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                    raise ApplyError("directory_changed", "a parent directory changed") from None
                raise ApplyError("open_failed", "cannot open a parent directory") from None
            os.close(parent)
            parent = nfd
            st = os.fstat(parent)
            try:
                _check_dir_trust(st, uid)
            except SnapshotError as exc:
                raise ApplyError("directory_changed", "a parent directory changed") from None
            parents.append((st.st_dev, st.st_ino))
        if tuple(parents) != opened.parents:
            raise ApplyError("directory_changed", "a directory in the source path changed")
        if hook:
            hook("before_reread")
        data, dev, ino, mode, owner = _read_file(parent, comps[-1], opened.dev, uid)
        if (data, dev, ino, mode, owner) != (opened.data, opened.dev, opened.ino, opened.mode, opened.uid):
            raise ApplyError("base_changed", "file changed since the preview")
        temp_name = ".gbbb-apply-" + secrets.token_hex(8) + ".tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        temp_fd = os.open(temp_name, flags, 0o600, dir_fd=parent)
        view = payload
        while view:
            wrote = os.write(temp_fd, view)
            if wrote <= 0:
                raise ApplyError("write_failed", "temporary file was not fully written")
            view = view[wrote:]
        os.fchmod(temp_fd, opened.mode)
        st = os.fstat(temp_fd)
        if st.st_uid != opened.uid:
            raise ApplyError("owner_not_preserved", "this process cannot create the replacement as the file owner")
        if st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != opened.mode or st.st_size != len(payload):
            raise ApplyError("temp_untrusted", "temporary file failed its own check")
        os.fsync(temp_fd)
        if hook:
            hook("before_rename")
        again, dev2, ino2, mode2, owner2 = _read_file(parent, comps[-1], opened.dev, uid)
        if (again, dev2, ino2, mode2, owner2) != (opened.data, opened.dev, opened.ino, opened.mode, opened.uid):
            raise ApplyError("base_changed", "file changed since the preview")
        os.rename(temp_name, comps[-1], src_dir_fd=parent, dst_dir_fd=parent)
        renamed = True
        temp_name = ""
        if hook:
            hook("after_rename")
        post = os.stat(comps[-1], dir_fd=parent, follow_symlinks=False)
        durable = "fsynced"
        try:
            os.fsync(parent)
        except OSError as exc:
            if exc.errno in (errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", 95)):
                durable = "directory_fsync_unsupported"
            else:
                durable = "fsync_failed"
        verified = stat.S_ISREG(post.st_mode) and post.st_nlink == 1 and post.st_ino == st.st_ino \
            and post.st_dev == st.st_dev and post.st_uid == uid and stat.S_IMODE(post.st_mode) == opened.mode \
            and post.st_size == len(payload)
        return {"renamed": True, "verified": verified, "durable": durable}
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_name and parent >= 0 and not renamed:
            try:
                os.unlink(temp_name, dir_fd=parent)
            except OSError:
                pass
        if parent >= 0:
            os.close(parent)
        os.close(root)


def _proposal_lineage(key: bytes, *, file: str, replacement: str, base_sha256: str | None,
                      proposal_token: str | None, replacement_format: str | None) -> tuple[str, str, str]:
    """Return lineage, proposal token, and format. A proposal token is not write permission."""
    if not proposal_token:
        if replacement_format:
            raise ApplyError("field_invalid", "replacement_format requires proposal_token")
        return "direct", "", ""
    if not replacement_format or not base_sha256:
        raise ApplyError("field_invalid", "proposal lineage requires base_sha256 and replacement_format")
    from .proposal import FORMATS, proposal_binding_token
    if replacement_format not in FORMATS:
        raise ApplyError("field_invalid", "replacement_format is not a known proposal format")
    expected = proposal_binding_token(
        key, fmt=replacement_format, selection=[file], base_sha256=[base_sha256],
        proposed_sha256=[hashlib.sha256(replacement.encode()).hexdigest()])
    if not hmac.compare_digest(proposal_token, expected):
        raise ApplyError("proposal_mismatch", "proposal token does not bind this path, base, and replacement")
    return "proposal", proposal_token, replacement_format


def run_apply(*, source: str, file: str, replacement: str, key: bytes, show_diff: bool = False,
              base_sha256: str | None = None, confirm_apply: str | None = None,
              proposal_token: str | None = None, replacement_format: str | None = None,
              hook: _HOOK | None = None) -> tuple[dict[str, Any], int]:
    """Preview when confirm_apply is absent. Write only when the apply token matches.

    Proposal lineage checks the reviewed base before any preview token is issued.
    Direct lineage does not claim that binding. Neither path treats proposal_token
    as permission to write.
    """
    lineage, bound_token, bound_format = _proposal_lineage(
        key, file=file, replacement=replacement, base_sha256=base_sha256,
        proposal_token=proposal_token, replacement_format=replacement_format)
    opened = _open_target(source, file)
    if lineage == "proposal" and opened.file != file:
        raise ApplyError("proposal_mismatch", "proposal token does not bind this path, base, and replacement")
    if confirm_apply is not None and not base_sha256:
        raise ApplyError("field_invalid", "confirm_apply requires base_sha256 from the preview")
    actual = hashlib.sha256(opened.data).hexdigest()
    if lineage == "proposal" and actual != base_sha256:
        raise ApplyError("proposal_base_changed", "file changed since the reviewed proposal")
    if base_sha256 is not None and base_sha256 != actual:
        raise ApplyError("base_changed", "file bytes do not match base_sha256")
    fields = dict(show_diff=show_diff, key=key, lineage=lineage, proposal_token=bound_token,
                  replacement_format=bound_format)
    if confirm_apply is None:
        body = _common(opened, replacement, **fields)
        body.update(mode="preview", applied=False, source_writes=False)
        return body, 0
    if not isinstance(confirm_apply, str) or not confirm_apply:
        raise ApplyError("field_invalid", "confirm_apply must be the preview token")
    expected = apply_token(key, opened, replacement, show_diff=show_diff, lineage=lineage,
                           proposal_token=bound_token, replacement_format=bound_format)
    if not hmac.compare_digest(confirm_apply, expected):
        raise ApplyError("consent_mismatch", "token does not match this file, path, and replacement")
    if opened.text == replacement:
        body = _common(opened, replacement, **fields)
        body.update(mode="apply", applied=False, source_writes=False, status="unchanged", durable=None)
        return body, 0
    try:
        outcome = _replace(opened, replacement, hook)
    except ApplyError:
        raise
    except OSError:
        raise ApplyError("io_error", "unexpected OSError (details withheld: may contain paths)") from None
    body = _common(opened, replacement, **fields)
    body.update(mode="apply", source_writes=True, durable=outcome["durable"],
                applied=bool(outcome["verified"]),
                status="applied" if outcome["verified"] else "apply_unverified")
    if not outcome["verified"]:
        body["error"] = "apply_unverified"
        return body, 3
    return body, 0


def replacement_text(obj: Any, file: str) -> str:
    """Strict one-file replacement object. Raises ApplyError."""
    try:
        found = validate_replacement_object(obj, [file])
    except ProposalError as exc:
        raise ApplyError(exc.code, exc.message) from None
    if list(found) != [file]:
        raise ApplyError("proposal_partial", "apply accepts exactly one file")
    return found[file]

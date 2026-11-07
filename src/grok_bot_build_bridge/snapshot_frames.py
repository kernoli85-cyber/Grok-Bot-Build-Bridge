"""Pure, bounded snapshot stream codec (schema gbbb.snapshot/v1). IN-MEMORY ONLY.

This module encodes/decodes a framed stream (manifest + file bytes) between two
file-like objects. It does NOT open source files, create directories, write to disk,
spawn wsl.exe, or copy repositories. The secure source opener and the receiver's
filesystem writer are NOT implemented yet (see docs/SNAPSHOT_TRANSFER.md); this codec
is the protocol layer only, tested adversarially with BytesIO.

Wire format (all integers big-endian):
  MAGIC                      b"GBBBSNAP\\x01\\n" (10 bytes)
  u32 manifest_len, manifest JSON (UTF-8, <= MAX_MANIFEST_BYTES):
      {"schema": "gbbb.snapshot/v1", "files": [{"path": str, "size": int}, ...]}
  for each manifest entry, in order:
      u64 length (must equal manifest size), <length> raw bytes, 32-byte SHA-256 of them
  END                        b"END\\x00" + u32 file_count + 32-byte SHA-256 of every
                             preceding byte of the stream (MAGIC..last file trailer)
  Nothing may follow END.
Hashes detect corruption/truncation on a local pipe; they are not authentication.
"""
from __future__ import annotations

import hashlib
import io
import json
import struct
import unicodedata
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterable

from .repo_plan import PlanRefused, normalize_selection

SCHEMA = "gbbb.snapshot/v1"
MAGIC = b"GBBBSNAP\x01\n"
END = b"END\x00"
CHUNK = 64 * 1024


@dataclass(frozen=True)
class FrameLimits:
    max_files: int = 200
    max_file_bytes: int = 256 * 1024
    max_total_bytes: int = 2 * 1024 * 1024
    max_manifest_bytes: int = 256 * 1024
    max_path_bytes: int = 1024


class FrameError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_manifest(files: list, lim: FrameLimits) -> list[tuple[str, int]]:
    if not isinstance(files, list) or not files:
        raise FrameError("manifest_empty", "manifest must list at least one file")
    if len(files) > lim.max_files:
        raise FrameError("too_many_files", f"more than {lim.max_files} files")
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    total = 0
    for f in files:
        if not isinstance(f, dict) or set(f) != {"path", "size"}:
            raise FrameError("manifest_entry_invalid", "entry must be exactly {path,size}")
        p, size = f["path"], f["size"]
        if not isinstance(p, str) or len(p.encode("utf-8", "surrogatepass")) > lim.max_path_bytes:
            raise FrameError("path_invalid", "path missing or too long")
        if unicodedata.normalize("NFC", p) != p:
            raise FrameError("path_not_nfc", "path must be NFC-normalized")
        try:
            norm = normalize_selection(p)
        except PlanRefused as exc:
            raise FrameError("path_refused", exc.code) from None
        if norm != p:
            raise FrameError("path_not_canonical", "path must be canonical")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > lim.max_file_bytes:
            raise FrameError("size_invalid", "size out of bounds")
        key = p.casefold()
        if key in seen:
            raise FrameError("duplicate_path", "duplicate path (case-insensitive)")
        seen.add(key)
        total += size
        if total > lim.max_total_bytes:
            raise FrameError("total_too_large", "total size exceeds bound")
        out.append((p, size))
    # A file may not also be a directory prefix of another file ("a" vs "a/b").
    for p in seen:
        parts = p.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in seen:
                raise FrameError("path_prefix_conflict", "a file path is also a directory of another")
    return out


def encode(files: Iterable[tuple[str, bytes]], out: BinaryIO, *, limits: FrameLimits = FrameLimits()) -> None:
    """Encode in-memory (path, bytes) pairs. Applies the same validation as decode."""
    items = list(files)
    manifest = [{"path": p, "size": len(b)} for p, b in items]
    _validate_manifest(manifest, limits)
    h = hashlib.sha256()

    def w(b: bytes) -> None:
        h.update(b)
        out.write(b)

    mj = json.dumps({"schema": SCHEMA, "files": manifest}, ensure_ascii=False,
                    separators=(",", ":")).encode("utf-8")
    if len(mj) > limits.max_manifest_bytes:
        raise FrameError("manifest_too_large", "manifest exceeds bound")
    w(MAGIC)
    w(struct.pack(">I", len(mj)) + mj)
    for _, data in items:
        w(struct.pack(">Q", len(data)))
        w(data)
        w(hashlib.sha256(data).digest())
    out.write(END + struct.pack(">I", len(items)) + h.digest())


class _Reader:
    def __init__(self, src: BinaryIO) -> None:
        self.src = src
        self.h = hashlib.sha256()

    def exact(self, n: int, *, hash_it: bool = True) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.src.read(min(CHUNK, n - len(buf)))
            if not chunk:
                raise FrameError("truncated", "stream ended early")
            buf += chunk
        b = bytes(buf)
        if hash_it:
            self.h.update(b)
        return b


Sink = Callable[[str, int], "FileSink"]


class FileSink:
    """Receiver-side writer interface. The in-memory one is the only implementation."""

    def write(self, chunk: bytes) -> None: ...

    def commit(self) -> None: ...

    def abort(self) -> None: ...


class MemorySink(FileSink):
    def __init__(self, store: dict, path: str) -> None:
        self.store, self.path, self.buf = store, path, bytearray()

    def write(self, chunk: bytes) -> None:
        self.buf += chunk

    def commit(self) -> None:
        self.store[self.path] = bytes(self.buf)

    def abort(self) -> None:
        self.buf.clear()


def decode(src: BinaryIO, *, limits: FrameLimits = FrameLimits(),
           sink_factory: Callable[[str, int], FileSink] | None = None) -> dict[str, bytes]:
    """Decode and verify a stream. All-or-nothing: on any error nothing is returned and
    every sink created so far is aborted (a disk sink would delete only its own temp file)."""
    store: dict[str, bytes] = {}
    factory = sink_factory or (lambda p, n: MemorySink(store, p))
    sinks: list[FileSink] = []
    r = _Reader(src)
    try:
        if r.exact(len(MAGIC)) != MAGIC:
            raise FrameError("bad_magic", "not a gbbb snapshot stream")
        (mlen,) = struct.unpack(">I", r.exact(4))
        if mlen == 0 or mlen > limits.max_manifest_bytes:
            raise FrameError("manifest_too_large", "manifest length out of bounds")
        try:
            m = json.loads(r.exact(mlen).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise FrameError("manifest_invalid", "manifest is not valid UTF-8 JSON") from None
        if not isinstance(m, dict) or set(m) != {"schema", "files"} or m["schema"] != SCHEMA:
            raise FrameError("manifest_invalid", "unexpected manifest schema")
        entries = _validate_manifest(m["files"], limits)
        for path, size in entries:
            (n,) = struct.unpack(">Q", r.exact(8))
            if n != size:
                raise FrameError("length_mismatch", "frame length differs from manifest")
            sink = factory(path, size)
            sinks.append(sink)
            fh = hashlib.sha256()
            left = n
            while left:
                chunk = r.exact(min(CHUNK, left))
                fh.update(chunk)
                sink.write(chunk)
                left -= len(chunk)
            if r.exact(32) != fh.digest():
                raise FrameError("file_hash_mismatch", "file content hash mismatch")
        stream_digest = r.h.digest()
        tail = r.exact(len(END) + 4 + 32, hash_it=False)
        if tail[:4] != END:
            raise FrameError("bad_end", "missing END frame")
        (count,) = struct.unpack(">I", tail[4:8])
        if count != len(entries) or tail[8:] != stream_digest:
            raise FrameError("stream_hash_mismatch", "END count/hash mismatch")
        if src.read(1):
            raise FrameError("trailing_data", "data after END")
    except FrameError:
        for s in sinks:
            s.abort()
        store.clear()
        raise
    for s in sinks:
        s.commit()
    return store


def roundtrip(files: list[tuple[str, bytes]], **kw) -> dict[str, bytes]:
    buf = io.BytesIO()
    encode(files, buf, **kw)
    buf.seek(0)
    return decode(buf, **kw)

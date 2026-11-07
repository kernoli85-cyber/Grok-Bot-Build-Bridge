"""Adversarial tests for the in-memory snapshot codec (no disk, no wsl.exe)."""
import hashlib
import io
import json
import struct

import pytest

from grok_bot_build_bridge.snapshot_frames import (END, MAGIC, SCHEMA, FrameError, FrameLimits,
                                                    MemorySink, decode, encode, roundtrip)

FILES = [("src/a.py", b"print(1)\n"), ("README.md", "Grüße 🙂\n".encode()), ("empty.txt", b"")]


def _enc(files=FILES, **kw):
    b = io.BytesIO()
    encode(files, b, **kw)
    return b.getvalue()


def _raw(manifest_files, bodies, *, fix_end=True, schema=SCHEMA):
    """Hand-build a stream (bypassing encoder validation) for adversarial cases."""
    h = hashlib.sha256()
    out = bytearray()

    def w(x):
        h.update(x)
        out.extend(x)
    mj = json.dumps({"schema": schema, "files": manifest_files}).encode()
    w(MAGIC)
    w(struct.pack(">I", len(mj)) + mj)
    for body in bodies:
        w(struct.pack(">Q", len(body)))
        w(body)
        w(hashlib.sha256(body).digest())
    out.extend(END + struct.pack(">I", len(bodies)) + (h.digest() if fix_end else b"\0" * 32))
    return bytes(out)


def _dec(data, **kw):
    return decode(io.BytesIO(data), **kw)


def test_roundtrip_unicode_and_empty():
    assert roundtrip(FILES) == dict(FILES)


@pytest.mark.parametrize("path,code", [
    ("../etc/passwd", "path_refused"), ("/abs", "path_refused"), ("C:/x", "path_refused"),
    ("a\\b", "path_refused"), ("a:ads", "path_refused"), (".git/config", "path_refused"),
    (".env", "path_refused"), ("con", "path_refused"), ("a/./b", "path_refused"),
    ("a/", "path_not_canonical"), ("cafe\u0301", "path_not_nfc"),
])
def test_receiver_refuses_bad_paths(path, code):
    data = _raw([{"path": path, "size": 1}], [b"x"])
    with pytest.raises(FrameError) as e:
        _dec(data)
    assert e.value.code == code


def test_duplicates_case_and_prefix_conflict():
    with pytest.raises(FrameError) as e:
        _dec(_raw([{"path": "A.txt", "size": 1}, {"path": "a.TXT", "size": 1}], [b"x", b"y"]))
    assert e.value.code == "duplicate_path"
    with pytest.raises(FrameError) as e:
        _dec(_raw([{"path": "a", "size": 1}, {"path": "a/b", "size": 1}], [b"x", b"y"]))
    assert e.value.code == "path_prefix_conflict"


def test_bounds():
    lim = FrameLimits(max_files=2, max_file_bytes=10, max_total_bytes=15)
    with pytest.raises(FrameError) as e:
        _dec(_enc([("a", b"1"), ("b", b"2"), ("c", b"3")]), limits=lim)
    assert e.value.code == "too_many_files"
    with pytest.raises(FrameError) as e:
        encode([("a", b"x" * 11)], io.BytesIO(), limits=lim)
    assert e.value.code == "size_invalid"
    with pytest.raises(FrameError) as e:
        _dec(_raw([{"path": "a", "size": 9}, {"path": "b", "size": 9}], [b"x" * 9, b"y" * 9]), limits=lim)
    assert e.value.code == "total_too_large"


def test_declared_huge_manifest_rejected_before_allocation():
    data = MAGIC + struct.pack(">I", 2**31)
    with pytest.raises(FrameError) as e:
        _dec(data)
    assert e.value.code == "manifest_too_large"


def test_length_lies_and_bomb_like_frames():
    # manifest says 1 byte, frame claims 2**60 bytes
    mj = json.dumps({"schema": SCHEMA, "files": [{"path": "a", "size": 1}]}).encode()
    data = MAGIC + struct.pack(">I", len(mj)) + mj + struct.pack(">Q", 2**60)
    with pytest.raises(FrameError) as e:
        _dec(data)
    assert e.value.code == "length_mismatch"


@pytest.mark.parametrize("cut", [5, 12, 40, -40, -1])
def test_truncation_anywhere(cut):
    data = _enc()
    with pytest.raises(FrameError) as e:
        _dec(data[:cut])
    assert e.value.code in {"truncated", "bad_magic", "manifest_invalid", "stream_hash_mismatch"}


def test_bitflip_in_content_detected():
    data = bytearray(_enc())
    i = data.index(b"print(1)")
    data[i] ^= 0x01
    with pytest.raises(FrameError) as e:
        _dec(bytes(data))
    assert e.value.code == "file_hash_mismatch"


def test_bad_end_hash_and_trailing_data():
    with pytest.raises(FrameError) as e:
        _dec(_raw([{"path": "a", "size": 1}], [b"x"], fix_end=False))
    assert e.value.code == "stream_hash_mismatch"
    with pytest.raises(FrameError) as e:
        _dec(_enc() + b"X")
    assert e.value.code == "trailing_data"


@pytest.mark.parametrize("manifest", [
    b"not json", b"\xff\xfe", b"[]", json.dumps({"schema": "other/v9", "files": []}).encode(),
    json.dumps({"schema": SCHEMA, "files": [{"path": "a", "size": 1, "mode": 0o4755}]}).encode(),
    json.dumps({"schema": SCHEMA, "files": [{"path": "a", "size": -1}]}).encode(),
    json.dumps({"schema": SCHEMA, "files": [{"path": "a", "size": True}]}).encode(),
    json.dumps({"schema": SCHEMA, "files": [], "extra": 1}).encode(),
])
def test_malformed_manifests(manifest):
    data = MAGIC + struct.pack(">I", len(manifest)) + manifest
    with pytest.raises(FrameError):
        _dec(data)


def test_bad_magic():
    with pytest.raises(FrameError) as e:
        _dec(b"PK\x03\x04" + b"\0" * 20)  # e.g. a zip/tar must never be accepted
    assert e.value.code == "bad_magic"


def test_all_or_nothing_aborts_sinks():
    events = []

    class Spy(MemorySink):
        def commit(self):
            events.append(("commit", self.path))

        def abort(self):
            events.append(("abort", self.path))
    data = bytearray(_enc())
    data[-40] ^= 1  # corrupt last file trailer/END region
    store = {}
    with pytest.raises(FrameError):
        decode(io.BytesIO(bytes(data)), sink_factory=lambda p, n: Spy(store, p))
    assert events and all(k == "abort" for k, _ in events) and store == {}


def test_reads_are_bounded_chunks():
    sizes = []

    class Src(io.BytesIO):
        def read(self, n=-1):
            assert n is not None and 0 < n <= 64 * 1024 or n == 1
            sizes.append(n)
            return super().read(n)
    decode(Src(_enc([("big.bin", b"z" * 200_000)], limits=FrameLimits(max_file_bytes=300_000))),
           limits=FrameLimits(max_file_bytes=300_000))
    assert max(sizes) <= 64 * 1024

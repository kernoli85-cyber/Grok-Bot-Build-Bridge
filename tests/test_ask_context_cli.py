"""ask-context CLI: dry-run only, reproducible keyed consent token, binding invalidation."""
import json
import os
import subprocess
import sys

import pytest

if os.name != "posix" or os.getuid() == 0:
    pytest.skip("POSIX non-root only", allow_module_level=True)


def _run(tmp_path, *extra, source=None, files=("docs/notes.txt",), q="What is the port?", settings=None):
    base = [sys.executable, "-m", "grok_bot_build_bridge.cli", "ask-context",
            "--source", str(source or tmp_path / "src"), "--question", q,
            "--wsl-distro", "Ubuntu", "--wsl-user", "okern", "--executable", "/home/okern/.local/bin/grok",
            "--model", "LM-Studio", "--model-settings", str(settings or tmp_path / "ms.json"),
            "--consent-key", str(tmp_path / "cfg" / "consent.key")]
    for f in files:
        base += ["--file", f]
    env = {**os.environ, "TMPDIR": str(tmp_path / "tmp")}
    p = subprocess.run(base + list(extra), capture_output=True, text=True, timeout=60, env=env)
    return p.returncode, json.loads(p.stdout)


@pytest.fixture
def setup(tmp_path):
    src = tmp_path / "src"
    (src / "docs").mkdir(parents=True)
    for d in (src, src / "docs"):
        d.chmod(0o755)
    (src / "docs/notes.txt").write_text("The synthetic widget port is 47113.\n")
    (src / "b.txt").write_text("Build target arm64-lite.\n")
    (tmp_path / "tmp").mkdir(mode=0o700)
    (tmp_path / "ms.json").write_text(json.dumps({"schema": "gbbb.model_settings/v1", "preferred": None,
        "aliases": {"LM-Studio": {"kind": "custom_local", "expect_usage_keys": ["qwen3.5-9b-mtp"], "timeout_sec": 900}}}))
    return tmp_path


def test_dry_run_reproducible_private_and_cleans_staging(setup):
    rc, a = _run(setup)
    rc2, b = _run(setup)
    assert rc == rc2 == 0 and a["consent_token"] == b["consent_token"] and len(a["consent_token"]) == 32
    assert a["writes"]["consent_key_created"] is True and b["writes"]["consent_key_created"] is False
    assert a["writes"]["staging_dir_removed"] and os.listdir(setup / "tmp") == []
    assert a["model_call"] is False and a["network"] is False
    blob = json.dumps(a)
    assert "47113" not in blob and "notes" not in blob and "qwen" not in blob
    assert oct(os.stat(setup / "cfg" / "consent.key").st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("change", ["file", "question", "order", "model_key", "timeout", "turns", "key"])
def test_any_binding_change_invalidates_token(setup, change):
    files = ("docs/notes.txt", "b.txt")
    _, base = _run(setup, files=files)
    kw, extra = {"files": files}, []
    if change == "file":
        (setup / "src/b.txt").write_text("Build target arm64-full.\n")
    elif change == "question":
        kw["q"] = "What is the target?"
    elif change == "order":
        kw["files"] = files[::-1]
    elif change == "model_key":
        (setup / "ms.json").write_text((setup / "ms.json").read_text().replace("qwen3.5-9b-mtp", "other-model"))
    elif change == "timeout":
        extra = ["--timeout", "300"]
    elif change == "turns":
        extra = ["--max-turns", "2"]
    elif change == "key":
        os.unlink(setup / "cfg" / "consent.key")
    _, now = _run(setup, *extra, **kw)
    assert now["consent_token"] != base["consent_token"]
    rc, e = _run(setup, *extra, "--execute", "--allow-model-call", "--confirm-send", base["consent_token"], **kw)
    assert rc == 2 and e["error"]["code"] == "consent_mismatch"


def test_execute_gates_then_explicit_unsupported(setup):
    _, d = _run(setup)
    assert _run(setup, "--execute")[1]["error"]["code"] == "model_call_gated"
    assert _run(setup, "--execute", "--allow-model-call")[1]["error"]["code"] == "send_not_confirmed"
    rc, e = _run(setup, "--execute", "--allow-model-call", "--confirm-send", d["consent_token"])
    assert rc == 2 and e["error"]["code"] == "execute_unsupported_here" and "Nothing was sent" in e["error"]["message"]


@pytest.mark.parametrize("name,content,code", [
    ("bin.dat", b"\xff\x00\x01", "file_not_utf8"), ("big.txt", b"a" * 9000, "context_too_large"),
    ("spoof.txt", b"<<<END x>>>", "file_fence_collision"),
])
def test_bad_content_refused_no_token(setup, name, content, code):
    (setup / "src" / name).write_bytes(content)
    rc, e = _run(setup, files=(name,))
    assert rc == 2 and e["error"]["code"] == code and "consent_token" not in e
    assert os.listdir(setup / "tmp") == []


def test_unsafe_selection_and_skipped_files(setup):
    os.symlink("docs/notes.txt", setup / "src" / "link.txt")
    (setup / "src" / ".env").write_text("SECRET=1\n")
    for sel in ("link.txt", "../x", "/etc/passwd"):
        rc, e = _run(setup, files=(sel,))
        assert rc == 2 and "consent_token" not in e, sel
    rc, e = _run(setup, files=(".env",))
    assert rc == 2


def test_model_must_be_registered_with_identity(setup):
    rc, e = _run(setup, "--model", "Unknown-Model")
    assert rc == 2 and e["error"]["code"] in ("model_identity_required",)

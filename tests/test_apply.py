"""Single-file apply: separate from the model, confirmed bytes only."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys

import pytest

from grok_bot_build_bridge.apply_file import ApplyError, run_apply
from grok_bot_build_bridge.context_pack import load_or_create_consent_key
from grok_bot_build_bridge.invoke import run_invoke
from grok_bot_build_bridge.state.paths import user_config_root

pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="POSIX non-root")
MODEL = {"id": "LM-Studio", "kind": "custom_local", "expect_usage_keys": ["provider-model-id"]}
MARKER = "UNIQUEAPPLYTEXT"


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    (tmp_path / "home").mkdir()


def _run(raw, capsys):
    code = run_invoke(raw if isinstance(raw, bytes) else json.dumps(raw).encode())
    return code, json.loads(capsys.readouterr().out)


def _tree(tmp_path, text="alpha\n", name="a.txt", mode=0o644):
    src = tmp_path / "src"
    (src / "notes").mkdir(parents=True)
    os.chmod(src, 0o755)
    os.chmod(src / "notes", 0o755)
    path = src / "notes" / name
    path.write_text(text)
    os.chmod(path, mode)
    return src, path


def _body(src, content, **extra):
    body = {
        "schema": "gbbb.invoke/v1", "op": "apply", "source": str(src), "file": "notes/a.txt",
        "replacement": {"schema": "gbbb.file_replacements/v1",
                        "files": [{"path": "notes/a.txt", "content": content}]},
    }
    body.update(extra)
    return body


def _key(tmp_path):
    key, _ = load_or_create_consent_key(str(user_config_root() / "consent.key"))
    return key


def test_preview_then_confirm_preserves_mode_and_reports_write(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path, f'def add(a, b):\n    """sum."""\n    return a - b\n')
    mode = os.stat(path).st_mode
    os.chmod(path, 0o755)
    mode = os.stat(path).st_mode
    content = path.read_text().replace("return a - b", "return a + b")
    code, preview = _run(_body(src, content), capsys)
    blob = json.dumps(preview)
    assert code == 0 and preview["mode"] == "preview" and preview["source_writes"] is False
    assert preview["lineage"] == "direct"
    assert preview["applied"] is False and preview["model_call"] is False and preview["retry"] is False
    assert preview["stored"] is False and preview["proposal_saved"] is False
    assert MARKER not in blob and "return a" not in blob and str(src) not in blob
    assert preview["limits"]["rollback"] == "none" and preview["limits"]["backup"] is False
    sent = _body(src, content, base_sha256=preview["base_sha256"], confirm_apply=preview["apply_token"],
                 show_diff=True)
    code, mismatch = _run(sent, capsys)
    assert code == 2 and mismatch["error"]["code"] == "consent_mismatch"
    assert path.read_text().endswith("return a - b\n")
    sent["show_diff"] = False
    code, done = _run(sent, capsys)
    assert code == 0 and done["source_writes"] is True and done["applied"] is True
    assert done["status"] == "applied" and done["durable"] in {"fsynced", "directory_fsync_unsupported"}
    assert path.read_text() == content and os.stat(path).st_mode == mode
    assert "return a + b" not in json.dumps(done)
    assert not list(src.rglob(".gbbb-apply-*.tmp"))
    assert not list((tmp_path / "home").rglob("*.sqlite"))


def test_stale_tampered_and_identical_do_not_write(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path)
    code, preview = _run(_body(src, "beta\n"), capsys)
    path.write_text("changed\n")
    sent = _body(src, "beta\n", base_sha256=preview["base_sha256"], confirm_apply=preview["apply_token"])
    code, bad = _run(sent, capsys)
    assert code == 2 and bad["error"]["code"] == "base_changed" and path.read_text() == "changed\n"
    path.write_text("alpha\n")
    tampered = _body(src, "gamma\n", base_sha256=preview["base_sha256"], confirm_apply=preview["apply_token"])
    code, bad = _run(tampered, capsys)
    assert code == 2 and bad["error"]["code"] == "consent_mismatch" and path.read_text() == "alpha\n"
    same = _body(src, "alpha\n", base_sha256=preview["base_sha256"], confirm_apply=preview["apply_token"])
    # token was for beta, not alpha
    code, bad = _run(same, capsys)
    assert code == 2 and bad["error"]["code"] == "consent_mismatch"
    code, preview = _run(_body(src, "alpha\n"), capsys)
    sent = _body(src, "alpha\n", base_sha256=preview["base_sha256"], confirm_apply=preview["apply_token"])
    code, done = _run(sent, capsys)
    assert code == 0 and done["status"] == "unchanged" and done["source_writes"] is False
    assert path.read_text() == "alpha\n"


def test_links_secrets_and_swaps_refuse(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path)
    link = src / "notes" / "link.txt"
    link.symlink_to(path.name)
    code, err = _run(_body(src, "beta\n", file="notes/link.txt", replacement={
        "schema": "gbbb.file_replacements/v1", "files": [{"path": "notes/link.txt", "content": "beta\n"}]}), capsys)
    assert code == 2 and err["error"]["code"] == "symlink" and path.read_text() == "alpha\n"
    other = src / "notes" / "b.txt"
    other.write_text("alpha\n")
    os.link(path, src / "notes" / "hard.txt")
    code, err = _run(_body(src, "beta\n", file="notes/hard.txt", replacement={
        "schema": "gbbb.file_replacements/v1", "files": [{"path": "notes/hard.txt", "content": "beta\n"}]}), capsys)
    assert code == 2 and err["error"]["code"] == "hardlink"
    os.unlink(src / "notes" / "hard.txt")
    code, err = _run(_body(src, "beta\n", file=".env", replacement={
        "schema": "gbbb.file_replacements/v1", "files": [{"path": ".env", "content": "beta\n"}]}), capsys)
    assert code == 2 and err["error"]["code"] == "selection_secret"
    os.chmod(path, 0o664)
    code, err = _run(_body(src, "beta\n"), capsys)
    assert code == 2 and err["error"]["code"] == "group_writable" and path.read_text() == "alpha\n"
    os.chmod(path, 0o644)
    os.chmod(src / "notes", 0o777)
    code, err = _run(_body(src, "beta\n"), capsys)
    assert code == 2 and err["error"]["code"] == "untrusted_dir"
    os.chmod(src / "notes", 0o755)
    code, preview = _run(_body(src, "beta\n"), capsys)
    os.rename(src / "notes", src / "notes-old")
    (src / "notes").mkdir()
    os.chmod(src / "notes", 0o755)
    swapped = src / "notes" / "a.txt"
    swapped.write_text("swapped\n")
    os.chmod(swapped, 0o644)
    sent = _body(src, "beta\n", base_sha256=preview["base_sha256"], confirm_apply=preview["apply_token"])
    code, err = _run(sent, capsys)
    assert code == 2 and err["error"]["code"] == "base_changed" and swapped.read_text() == "swapped\n"


def test_fault_injection_cleans_temp_and_does_not_roll_back(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path, "alpha\n")
    key = _key(tmp_path)
    preview, code = _invoke_direct(src, "beta\n", key)
    assert code == 0

    def mutate(stage):
        if stage == "before_rename":
            path.write_text("mutated\n")

    with pytest.raises(ApplyError) as exc:
        run_apply(source=str(src), file="notes/a.txt", replacement="beta\n", key=key,
                  base_sha256=preview["base_sha256"], confirm_apply=preview["apply_token"], hook=mutate)
    assert exc.value.code == "base_changed" and path.read_text() == "mutated\n"
    assert not list(src.rglob(".gbbb-apply-*.tmp"))
    path.write_text("alpha\n")
    preview, code = _invoke_direct(src, "beta\n", key)
    assert code == 0

    def boom(stage):
        if stage == "before_rename":
            raise ApplyError("injected", "stop before rename")

    with pytest.raises(ApplyError) as exc:
        run_apply(source=str(src), file="notes/a.txt", replacement="beta\n", key=key,
                  base_sha256=preview["base_sha256"], confirm_apply=preview["apply_token"], hook=boom)
    assert path.read_text() == "alpha\n" and not list(src.rglob(".gbbb-apply-*.tmp"))

    def swap_after(stage):
        if stage == "after_rename":
            os.remove(path)
            path.write_text("other\n")
            os.chmod(path, 0o644)

    body, status = run_apply(source=str(src), file="notes/a.txt", replacement="beta\n", key=key,
                             base_sha256=preview["base_sha256"], confirm_apply=preview["apply_token"],
                             hook=swap_after)
    assert status == 3 and body["source_writes"] is True and body["applied"] is False
    assert body["status"] == "apply_unverified" and path.read_text() == "other\n"


def _invoke_direct(src, content, key):
    return run_apply(source=str(src), file="notes/a.txt", replacement=content, key=key)


def test_show_diff_and_unknown_fields(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path, MARKER + "\n")
    code, shown = _run(_body(src, "beta\n", show_diff=True), capsys)
    assert code == 0 and MARKER in shown["diff"] and shown["source_writes"] is False
    bad = _body(src, "beta\n", execute=True)
    code, err = _run(bad, capsys)
    assert code == 2 and err["error"]["code"] == "schema_invalid" and path.read_text().startswith(MARKER)
    wide = _ask_too_big()
    code, err = _run(wide, capsys)
    assert code == 2 and err["error"]["code"] == "envelope_too_large"


def _ask_too_big():
    return {
        "schema": "gbbb.invoke/v1", "op": "ask", "executable": "/usr/bin/grok",
        "model": dict(MODEL), "prompt": "x" * 20000,
    }


def test_return_replacement_is_opt_in_and_not_an_apply_token(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path, "alpha\n")
    before = path.read_bytes()
    bindir = tmp_path / "bin"
    bindir.mkdir(mode=0o755)
    dummy = bindir / "grok"
    replacement = json.dumps({"schema": "gbbb.file_replacements/v1",
                              "files": [{"path": "notes/a.txt", "content": "beta\n"}]})
    dummy.write_text("#!" + sys.executable + "\nimport json, os, sys\n"
                     "assert '--sandbox' in sys.argv and os.listdir('.') == []\n"
                     "print(json.dumps(" + json.dumps({
                         "text": replacement, "stopReason": "end_turn", "num_turns": 1,
                         "usage": {"input_tokens": 1, "output_tokens": 1},
                         "modelUsage": {"provider-model-id": {}},
                     }) + "))\n")
    dummy.chmod(0o755)
    base = {
        "schema": "gbbb.invoke/v1", "op": "propose", "executable": str(dummy), "model": dict(MODEL),
        "source": str(src), "files": ["notes/a.txt"], "instruction": "Use beta.",
    }
    code, dry = _run(base, capsys)
    sent = dict(base, execute=True, allow_model_call=True, confirm_send=dry["consent_token"])
    code, done = _run(sent, capsys)
    assert code == 0 and "replacement" not in done["proposal"] and path.read_bytes() == before
    flagged = dict(base, return_replacement=True)
    code, dry = _run(flagged, capsys)
    assert "beta" not in json.dumps(dry)
    sent = dict(flagged, execute=True, allow_model_call=True, confirm_send=dry["consent_token"])
    code, done = _run(sent, capsys)
    assert code == 0 and done["proposal"]["replacement"]["files"][0]["content"] == "beta\n"
    assert done["proposal"]["base_sha256"] == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before
    stolen = _body(src, "beta\n", confirm_apply=done["proposal"]["proposal_token"],
                   base_sha256=hashlib.sha256(before).hexdigest())
    code, err = _run(stolen, capsys)
    assert code == 2 and err["error"]["code"] == "consent_mismatch" and path.read_bytes() == before
    many = dict(base, files=["notes/a.txt", "notes/b.txt"], return_replacement=True)
    code, err = _run(many, capsys)
    assert code == 2 and err["error"]["code"] == "field_invalid"


def test_reviewed_proposal_refuses_a_changed_base_before_preview(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path, "alpha\n")
    before = path.read_bytes()
    owner = os.stat(path).st_uid
    bindir = tmp_path / "bin"
    bindir.mkdir(mode=0o755)
    dummy = bindir / "grok"
    replacement = json.dumps({"schema": "gbbb.file_replacements/v1",
                              "files": [{"path": "notes/a.txt", "content": "beta\n"}]})
    dummy.write_text("#!" + sys.executable + "\nimport json, os, sys\n"
                     "assert '--sandbox' in sys.argv\n"
                     "print(json.dumps(" + json.dumps({
                         "text": replacement, "stopReason": "end_turn", "num_turns": 1,
                         "usage": {"input_tokens": 1, "output_tokens": 1},
                         "modelUsage": {"provider-model-id": {}},
                     }) + "))\n")
    dummy.chmod(0o755)
    propose = {
        "schema": "gbbb.invoke/v1", "op": "propose", "executable": str(dummy), "model": dict(MODEL),
        "source": str(src), "files": ["notes/a.txt"], "instruction": "Use beta.",
        "return_replacement": True,
    }
    code, dry = _run(propose, capsys)
    sent = dict(propose, execute=True, allow_model_call=True, confirm_send=dry["consent_token"])
    code, done = _run(sent, capsys)
    assert code == 0 and done["proposal"]["base_sha256"] == hashlib.sha256(before).hexdigest()
    reviewed = done["proposal"]["replacement"]
    token = done["proposal"]["proposal_token"]
    path.write_text("newer\n")
    stale = _body(src, "beta\n", replacement=reviewed, base_sha256=done["proposal"]["base_sha256"],
                  proposal_token=token, replacement_format="json-v1")
    code, err = _run(stale, capsys)
    assert code == 2 and err["error"]["code"] == "proposal_base_changed"
    assert "apply_token" not in err and path.read_text() == "newer\n"
    rebased = dict(stale, base_sha256=hashlib.sha256(b"newer\n").hexdigest())
    code, err = _run(rebased, capsys)
    assert code == 2 and err["error"]["code"] == "proposal_mismatch" and path.read_text() == "newer\n"
    direct = _body(src, "beta\n")
    code, preview = _run(direct, capsys)
    assert code == 0 and preview["lineage"] == "direct" and preview["source_writes"] is False
    assert path.read_text() == "newer\n"
    path.write_text("alpha\n")
    fresh = _body(src, "beta\n", replacement=reviewed, base_sha256=done["proposal"]["base_sha256"],
                  proposal_token=token, replacement_format="json-v1")
    code, preview = _run(fresh, capsys)
    assert code == 0 and preview["lineage"] == "proposal" and preview["source_writes"] is False
    stolen = dict(fresh, confirm_apply=token)
    code, err = _run(stolen, capsys)
    assert code == 2 and err["error"]["code"] == "consent_mismatch" and path.read_bytes() == before
    fresh["confirm_apply"] = preview["apply_token"]
    code, applied = _run(fresh, capsys)
    assert code == 0 and applied["source_writes"] is True and applied["applied"] is True
    assert path.read_text() == "beta\n" and os.stat(path).st_uid == owner
    os.chmod(path, 0o755)
    mode = os.stat(path).st_mode
    again = _body(src, "gamma\n")
    code, preview = _run(again, capsys)
    again.update(base_sha256=preview["base_sha256"], confirm_apply=preview["apply_token"])
    code, applied = _run(again, capsys)
    assert code == 0 and os.stat(path).st_mode == mode and os.stat(path).st_uid == owner
    os.chmod(path, 0o644)
    os.chmod(path, stat.S_ISUID | 0o644)
    if os.stat(path).st_mode & stat.S_ISUID:
        code, err = _run(_body(src, "delta\n"), capsys)
        assert code == 2 and err["error"]["code"] == "special_mode"
        assert path.read_text() == "gamma\n"

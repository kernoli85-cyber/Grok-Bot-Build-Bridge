"""propose op: reviewable diff only, source bytes unchanged."""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from grok_bot_build_bridge.invoke import run_invoke
from grok_bot_build_bridge.runner import posix_local as PL

MODEL = {"id": "LM-Studio", "kind": "custom_local", "expect_usage_keys": ["provider-model-id"]}
pytestmark = pytest.mark.skipif(os.name != "posix" or os.getuid() == 0, reason="POSIX non-root")


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))


def _source(tmp_path, text="alpha\n"):
    src = tmp_path / "src"
    (src / "notes").mkdir(parents=True)
    os.chmod(src, 0o755)
    os.chmod(src / "notes", 0o755)
    path = src / "notes" / "a.txt"
    path.write_text(text)
    return src, path


def _dummy(tmp_path, text, model="provider-model-id"):
    bindir = tmp_path / "bin"
    bindir.mkdir(mode=0o755, parents=True)
    dummy = bindir / "grok"
    grok = {"text": text, "stopReason": "end_turn", "num_turns": 1,
            "usage": {"input_tokens": 1, "output_tokens": 1}, "modelUsage": {model: {}}}
    dummy.write_text("#!" + sys.executable + "\nimport json, os, sys\n"
                     "assert '--sandbox' in sys.argv and 'MCPTool' in sys.argv and os.listdir('.') == []\n"
                     "print(json.dumps(" + json.dumps(grok) + "))\n")
    dummy.chmod(0o755)
    return dummy


def _run(raw, capsys):
    code = run_invoke(raw if isinstance(raw, bytes) else json.dumps(raw).encode())
    return code, json.loads(capsys.readouterr().out)


def _body(dummy, src, **extra):
    body = {
        "schema": "gbbb.invoke/v1", "op": "propose", "executable": str(dummy),
        "model": dict(MODEL), "source": str(src), "files": ["notes/a.txt"],
        "instruction": "Use the word beta.",
    }
    body.update(extra)
    return body


def _replacement(content):
    return json.dumps({"schema": "gbbb.file_replacements/v1",
                       "files": [{"path": "notes/a.txt", "content": content}]})


def test_dry_run_execute_diff_does_not_write_source(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _source(tmp_path)
    before = path.read_bytes()
    dummy = _dummy(tmp_path, _replacement("beta\n"))
    code, dry = _run(_body(dummy, src), capsys)
    assert code == 0 and dry["proposal_only"] is True and dry["applied"] is False and dry["source_writes"] is False
    assert "alpha" not in json.dumps(dry) and "beta" not in json.dumps(dry)
    sent = _body(dummy, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"],
                 show_proposal=True)
    code, done = _run(sent, capsys)
    assert code == 0 and done["proposal"]["accepted"] is True and done["proposal"]["changed"] is True
    assert done["proposal"]["applied"] is False and done["source_writes"] is False and done["stored"] is False
    assert done["proposal"]["files"][0]["diff"] and "beta" in done["proposal"]["files"][0]["diff"]
    assert path.read_bytes() == before
    assert len(done["proposal"]["proposal_token"]) == 32


def test_no_change_and_rejected_text_leave_bytes(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _source(tmp_path, "alpha\n")
    before = path.read_bytes()
    dummy = _dummy(tmp_path, _replacement("alpha\n"))
    _code, dry = _run(_body(dummy, src), capsys)
    sent = _body(dummy, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"],
                 show_proposal=True)
    code, done = _run(sent, capsys)
    assert code == 0 and done["proposal"]["changed"] is False and done["proposal"]["files"][0]["diff"] == ""
    assert path.read_bytes() == before
    for text, error in (
        ("{nope", "proposal_malformed"),
        (json.dumps({"schema": "nope", "files": []}), "proposal_wrong_schema"),
        ("```\n" + _replacement("beta\n") + "\n```", "proposal_chatter"),
        (_replacement("beta\n") + _replacement("beta\n"), "proposal_duplicate_json"),
        (_replacement_other(), "proposal_wrong_path"),
        ("", "proposal_empty"),
        (_replacement("x" * 9000), "proposal_oversize"),
    ):
        dummy2 = _dummy(tmp_path / error, text)
        src2, path2 = _source(tmp_path / error)
        prior = path2.read_bytes()
        _c, dry2 = _run(_body(dummy2, src2), capsys)
        sent2 = _body(dummy2, src2, execute=True, allow_model_call=True, confirm_send=dry2["consent_token"])
        code2, bad = _run(sent2, capsys)
        assert code2 == 3 and bad["proposal"]["accepted"] is False and bad["proposal"]["error"] == error
        assert "proposal_token" not in bad["proposal"] and path2.read_bytes() == prior


def _replacement_other():
    return json.dumps({"schema": "gbbb.file_replacements/v1",
                       "files": [{"path": "notes/missing.txt", "content": "z\n"}]})


def test_token_misses_when_source_changes_before_execute(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _source(tmp_path)
    dummy = _dummy(tmp_path, _replacement("beta\n"))
    _c, dry = _run(_body(dummy, src), capsys)
    path.write_text("external\n")
    sent = _body(dummy, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"])
    code, bad = _run(sent, capsys)
    assert code == 2 and bad["error"]["code"] == "consent_mismatch"
    assert path.read_text() == "external\n" and "beta" not in path.read_text()


def test_change_during_call_is_not_applied(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _source(tmp_path)
    dummy = _dummy(tmp_path, _replacement("beta\n"))
    _c, dry = _run(_body(dummy, src), capsys)
    real_stage = __import__("grok_bot_build_bridge.proposal", fromlist=["stage_proposal"]).stage_proposal
    calls = {"n": 0}

    def wrapped(source, files, instruction, key):
        calls["n"] += 1
        if calls["n"] >= 2:
            path.write_text("during\n")
        return real_stage(source, files, instruction, key)

    monkeypatch.setattr("grok_bot_build_bridge.proposal.stage_proposal", wrapped)
    sent = _body(dummy, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"])
    code, bad = _run(sent, capsys)
    assert code == 3 and bad["proposal"]["error"] == "source_changed"
    assert path.read_text() == "during\n" and "beta" not in path.read_text()


def test_identity_mismatch_and_cleanup_failure_do_not_apply(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _source(tmp_path)
    before = path.read_bytes()
    mismatch = _dummy(tmp_path / "mismatch", _replacement("beta\n"), model="other-model")
    _c, dry = _run(_body(mismatch, src), capsys)
    sent = _body(mismatch, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"])
    code, bad = _run(sent, capsys)
    assert code == 3 and bad["result"]["model_identity"] == "mismatch"
    assert "proposal" not in bad and path.read_bytes() == before

    monkeypatch.setattr(PL, "_kill_group", lambda _pgid: "unproven")
    ok = _dummy(tmp_path / "clean", _replacement("beta\n"))
    _c, dry = _run(_body(ok, src), capsys)
    sent = _body(ok, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"])
    code, bad = _run(sent, capsys)
    assert code == 3 and bad["result"]["error_code"] == "cleanup_unproven"
    assert "proposal" not in bad and bad["applied"] is False and path.read_bytes() == before
    assert stat.S_ISREG(path.stat().st_mode)

"""stdin invoke envelope: strict schema, consent, POSIX runner, no stored content."""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from grok_bot_build_bridge.invoke import run_invoke

MODEL = {"id": "LM-Studio", "kind": "custom_local", "expect_usage_keys": ["provider-model-id"]}
PROMPT = "PROMPTTOKEN ready"


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))


def _ask(**extra):
    body = {
        "schema": "gbbb.invoke/v1", "op": "ask",
        "executable": "/home/gbbbdemo/.local/bin/grok",
        "model": dict(MODEL), "prompt": PROMPT,
    }
    body.update(extra)
    return json.dumps(body).encode()


def _run(raw, capsys):
    code = run_invoke(raw)
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_rejects_unknown_fields_shell_and_widening(capsys):
    cases = [
        _ask(env={"HOME": "/tmp"}),
        _ask(executable="/usr/bin/grok;id"),
        _ask(executable="grok"),
        _ask(max_turns=2),
        _ask(model={"id": "LM-Studio", "kind": "builtin", "expect_usage_keys": ["provider-model-id"]}),
        _ask(execute=True),
        b'{"schema":"gbbb.invoke/v1"}{"schema":"gbbb.invoke/v1"}',
        b'{"schema": "gbbb.invoke/v1", "schema": "gbbb.invoke/v1"}',
        _ask(prompt="PROMPTTOKEN") + b"\xff",
    ]
    # last case is utf-8 invalid only if we append a bad byte to valid utf-8
    codes = []
    for raw in cases[:-1]:
        code, body = _run(raw, capsys)
        codes.append(body["error"]["code"])
        assert code == 2
    code, body = _run(cases[-1], capsys)
    assert code == 2 and body["error"]["code"] == "envelope_not_utf8"
    assert codes[0] == "schema_invalid"
    assert codes[1] == "field_invalid"
    assert codes[2] == "field_invalid"
    assert codes[3] == "field_invalid"
    assert codes[4] == "model_identity_required"
    assert codes[5] == "model_call_gated"
    assert codes[6] == "envelope_invalid"
    assert codes[7] == "envelope_invalid"
    assert "PROMPTTOKEN" not in json.dumps(codes)


@pytest.mark.skipif(os.name != "posix" or os.getuid() == 0, reason="POSIX non-root")
def test_ask_dry_run_then_execute_dummy_stores_nothing(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir(mode=0o755)
    dummy = bindir / "grok"
    dummy.write_text(
        "#!" + sys.executable + "\nimport json, os, sys\n"
        "assert '--sandbox' in sys.argv and '--deny' in sys.argv and 'MCPTool' in sys.argv\n"
        "assert os.listdir('.') == []\n"
        "print(json.dumps({'text': 'ok', 'stopReason': 'end_turn', 'num_turns': 1, "
        "'sessionId': 'sess-SHOULD-NOT-LEAK', "
        "'usage': {'input_tokens': 1, 'output_tokens': 1}, "
        "'modelUsage': {'provider-model-id': {}}}))\n")
    dummy.chmod(0o755)
    code, dry = _run(_ask(executable=str(dummy)), capsys)
    assert code == 0 and dry["model_call"] is False and dry["stored"] is False
    assert "PROMPTTOKEN" not in json.dumps(dry)
    assert len(dry["consent_token"]) == 32
    key = tmp_path / "config" / "grok-bot-build-bridge" / "consent.key"
    assert key.is_file() and stat.S_IMODE(key.stat().st_mode) == 0o600
    sent = json.loads(_ask(executable=str(dummy)))
    sent.update(execute=True, allow_model_call=True, confirm_send="0" * 32)
    code, bad = _run(json.dumps(sent).encode(), capsys)
    assert code == 2 and bad["error"]["code"] == "consent_mismatch"
    sent["confirm_send"] = dry["consent_token"]
    code, done = _run(json.dumps(sent).encode(), capsys)
    blob = json.dumps(done)
    assert code == 0 and done["result"]["status"] == "succeeded", blob
    assert done["result"]["model_identity"] == "confirmed" and done["retry"] is False
    assert done["stored"] is False and "sess-SHOULD-NOT-LEAK" not in blob and "PROMPTTOKEN" not in blob
    assert not list((tmp_path / "home").rglob("*.sqlite"))


@pytest.mark.skipif(os.name != "posix" or os.getuid() == 0, reason="POSIX non-root")
def test_context_dry_run_hides_names_and_rejects_windows_source_shape(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src = tmp_path / "src"
    (src / "notes").mkdir(parents=True)
    os.chmod(src, 0o755)
    os.chmod(src / "notes", 0o755)
    (src / "notes" / "a.txt").write_text("synthetic value 47113\n")
    body = {
        "schema": "gbbb.invoke/v1", "op": "ask-context",
        "executable": "/home/gbbbdemo/.local/bin/grok",
        "model": dict(MODEL), "source": str(src), "files": ["notes/a.txt"],
        "question": "QUESTIONTOKEN port?",
    }
    code, dry = _run(json.dumps(body).encode(), capsys)
    blob = json.dumps(dry)
    assert code == 0 and dry["mode"] == "dry_run" and "47113" not in blob and "QUESTIONTOKEN" not in blob
    assert "notes" not in blob and dry["context"]["files"] == 1
    changed = dict(body, question="other question")
    code2, dry2 = _run(json.dumps(changed).encode(), capsys)
    assert code2 == 0 and dry2["consent_token"] != dry["consent_token"]
    bad = dict(body, source=r"C:\Users\x\proj")
    code3, err = _run(json.dumps(bad).encode(), capsys)
    assert code3 == 2 and "consent_token" not in err and "Users" not in json.dumps(err)


def test_windows_or_any_platform_does_not_require_listener(monkeypatch, tmp_path, capsys):
    """Schema failures are local. POSIX-only is a JSON error, not a socket."""
    _env(monkeypatch, tmp_path)
    if os.name != "posix" or sys.platform == "win32":
        code, body = _run(_ask(), capsys)
        assert code == 2 and body["error"]["code"] == "posix_only"
        assert not (tmp_path / "config").exists()
    else:
        code, body = _run(b"{", capsys)
        assert code == 2 and body["error"]["code"] == "envelope_invalid"

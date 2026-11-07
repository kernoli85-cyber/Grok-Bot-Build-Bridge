"""Missing final LF is visible in propose and apply diffs. Bytes stay exact."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

from grok_bot_build_bridge.invoke import run_invoke
from grok_bot_build_bridge.proposal import (
    ProposalError, parse_replacements, proposal_token, render_unified_diff, review,
)

pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="POSIX non-root")
MODEL = {"id": "LM-Studio", "kind": "custom_local", "expect_usage_keys": ["provider-model-id"]}
SRC = "def add(a,b):\n    return a-b\n"
REPL = "def add(a,b):\n    return a+b"
MARK = "\\ No newline at end of file"
RICH = '"""doc."""\nNOTE = r"line\\nbreak"\n'


def test_renderer_shows_every_final_lf_difference_and_does_not_rewrite():
    base, repl = SRC, REPL
    missing = render_unified_diff("calc.py", base, repl)
    assert MARK in missing and "return a-b" in missing and "return a+b" in missing
    assert missing.index("return a-b") < missing.index(MARK)
    added = render_unified_diff("calc.py", repl, base)
    assert MARK in added and added.index(MARK) < added.index("return a-b")
    assert MARK not in render_unified_diff("calc.py", SRC, SRC.replace("a-b", "a+b"))
    assert render_unified_diff("calc.py", REPL, REPL) == ""
    assert render_unified_diff("calc.py", "a\n\n", "a\n\n") == ""
    blank = render_unified_diff("calc.py", "a\n\n", "a\n")
    assert "\n-\n" in blank and MARK not in blank
    rich_repl = RICH[:-1].replace("break", "keep")
    rich = render_unified_diff("calc.py", RICH, rich_repl)
    assert '"""doc."""' in rich and 'line\\n' in rich and MARK in rich
    assert base == SRC and repl == REPL
    shown = render_unified_diff("calc.py", "a\r\n", "a\n")
    assert "\r" in shown


def test_cr_is_still_rejected_before_any_diff():
    raw = json.dumps({"schema": "gbbb.file_replacements/v1",
                      "files": [{"path": "calc.py", "content": "a\r\n"}]})
    with pytest.raises(ProposalError) as exc:
        parse_replacements(raw, ["calc.py"])
    assert exc.value.code == "proposal_not_utf8"


def test_proposal_diff_marks_removed_lf_and_token_binds_exact_bytes():
    key = b"k" * 32
    bases = {"calc.py": SRC}
    proposed = {"calc.py": REPL}
    shown = review(["calc.py"], bases, proposed, key, show=True, fmt="json-v1")
    hidden = review(["calc.py"], bases, proposed, key, show=False, fmt="json-v1")
    assert MARK in shown["files"][0]["diff"]
    assert shown["proposal_token"] == hidden["proposal_token"]
    assert shown["proposal_token"] == proposal_token(key, ["calc.py"], bases, proposed, fmt="json-v1")
    assert hashlib.sha256(REPL.encode()).hexdigest() != hashlib.sha256((REPL + "\n").encode()).hexdigest()


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    (tmp_path / "home").mkdir()


def _run(raw, capsys):
    code = run_invoke(json.dumps(raw).encode())
    return code, json.loads(capsys.readouterr().out)


def test_propose_and_apply_preview_show_missing_lf_without_writing(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    os.chmod(src, 0o755)
    path = src / "calc.py"
    path.write_bytes(SRC.encode())
    before = path.read_bytes()
    bindir = tmp_path / "bin"
    bindir.mkdir(mode=0o755)
    dummy = bindir / "grok"
    payload = json.dumps({"schema": "gbbb.file_replacements/v1",
                          "files": [{"path": "calc.py", "content": REPL}]})
    dummy.write_text(
        "#!" + sys.executable + "\nimport json, os, sys\n"
        "assert '--sandbox' in sys.argv and os.listdir('.') == []\n"
        "print(json.dumps(" + json.dumps({
            "text": payload, "stopReason": "end_turn", "num_turns": 1,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {"provider-model-id": {}},
        }) + "))\n")
    dummy.chmod(0o755)
    propose = {
        "schema": "gbbb.invoke/v1", "op": "propose", "executable": str(dummy),
        "model": dict(MODEL), "source": str(src), "files": ["calc.py"],
        "instruction": "Change minus to plus.", "return_replacement": True,
    }
    code, dry = _run(propose, capsys)
    sent = dict(propose, execute=True, allow_model_call=True, confirm_send=dry["consent_token"],
                show_proposal=True)
    code, done = _run(sent, capsys)
    assert code == 0 and MARK in done["proposal"]["files"][0]["diff"]
    assert done["proposal"]["replacement"]["files"][0]["content"] == REPL
    assert path.read_bytes() == before
    token = done["proposal"]["proposal_token"]
    base_sha = done["proposal"]["base_sha256"]
    apply_body = {
        "schema": "gbbb.invoke/v1", "op": "apply", "source": str(src), "file": "calc.py",
        "show_diff": True, "replacement_format": "json-v1", "proposal_token": token,
        "base_sha256": base_sha, "replacement": done["proposal"]["replacement"],
    }
    code, preview = _run(apply_body, capsys)
    code2, again = _run(apply_body, capsys)
    assert code == 0 and preview["source_writes"] is False and preview["lineage"] == "proposal"
    assert MARK in preview["diff"] and preview["apply_token"] == again["apply_token"]
    assert path.read_bytes() == before
    apply_body["confirm_apply"] = preview["apply_token"]
    apply_body["show_diff"] = False
    code, bad = _run(apply_body, capsys)
    assert code == 2 and bad["error"]["code"] == "consent_mismatch" and path.read_bytes() == before
    apply_body["show_diff"] = True
    code, wrote = _run(apply_body, capsys)
    assert code == 0 and wrote["source_writes"] is True and path.read_bytes() == REPL.encode()
    assert not path.read_bytes().endswith(b"\n")

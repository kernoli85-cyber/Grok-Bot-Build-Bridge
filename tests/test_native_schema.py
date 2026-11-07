"""native-schema-v1: bridge-owned flags, structuredOutput only, source unchanged."""
from __future__ import annotations

import json
import os
import sys

import pytest

from grok_bot_build_bridge.invoke import run_invoke
from grok_bot_build_bridge.proposal import NATIVE_SYSTEM_PROMPT, native_replacement_schema
from grok_bot_build_bridge.runner import posix_local as PL
from grok_bot_build_bridge.runner.grok_result import parse_grok_json_result
from grok_bot_build_bridge.runner.plan import build_wsl_grok_ask_plan

MODEL = {"id": "LM-Studio", "kind": "custom_local", "expect_usage_keys": ["provider-model-id"]}
pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="POSIX non-root")
CALC = 'def add(a, b):\n    """sum."""\n    return a - b\n'
PROPOSED = CALC.replace("return a - b", "return a + b")
CHATTER = "I edited calc.py and ran the tests."


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))


def _tree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    os.chmod(src, 0o755)
    path = src / "calc.py"
    path.write_text(CALC)
    return src, path


def _dummy(tmp_path, obj):
    bindir = tmp_path / "bin"
    bindir.mkdir(mode=0o755)
    dummy = bindir / "grok"
    calls = tmp_path / "calls"
    argv_path = tmp_path / "argv.json"
    dummy.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "calls = " + repr(str(calls)) + "\n"
        "n = int(open(calls).read()) if os.path.exists(calls) else 0\n"
        "open(calls, 'w').write(str(n + 1))\n"
        "open(" + repr(str(argv_path)) + ", 'w').write(json.dumps(sys.argv))\n"
        "need = ['--verbatim', '--system-prompt-override', '--json-schema', '--sandbox', 'strict',\n"
        "        '--permission-mode', 'dontAsk', '--disable-web-search', '--no-subagents', '--no-plan']\n"
        "assert all(item in sys.argv for item in need)\n"
        "assert sys.argv[sys.argv.index('--max-turns') + 1] == '1'\n"
        "assert sys.argv.count('--single') == 1 and sys.argv.count('--deny') == 7\n"
        "for banned in ('--rules', '--prompt-file', '--prompt-json', '--append-system-prompt', '--always-approve'):\n"
        "    assert banned not in sys.argv\n"
        "assert os.listdir('.') == []\n"
        "print(json.dumps(" + json.dumps(obj) + "))\n"
    )
    dummy.chmod(0o755)
    return dummy, calls, argv_path


def _run(raw, capsys):
    code = run_invoke(raw if isinstance(raw, bytes) else json.dumps(raw).encode())
    return code, json.loads(capsys.readouterr().out)


def _body(dummy, src, **extra):
    body = {
        "schema": "gbbb.invoke/v1", "op": "propose", "executable": str(dummy),
        "model": dict(MODEL), "source": str(src), "files": ["calc.py"],
        "instruction": "Change add to return a + b. Keep the docstring.",
        "replacement_format": "native-schema-v1",
    }
    body.update(extra)
    return body


def _base_result(**extra):
    out = {"stopReason": "end_turn", "num_turns": 1,
           "usage": {"input_tokens": 1, "output_tokens": 1},
           "modelUsage": {"provider-model-id": {}}}
    out.update(extra)
    return out


def _value(content=PROPOSED, path="calc.py", extra_file=None):
    row = {"path": path, "content": content}
    if extra_file:
        row.update(extra_file)
    return {"schema": "gbbb.file_replacements/v1", "files": [row]}


def test_ask_argv_has_no_schema_and_native_keeps_denies():
    schema = native_replacement_schema(["calc.py"])
    from grok_bot_build_bridge.model_select import ModelChoice
    choice = ModelChoice("LM-Studio", "per_job", "custom_local", ("provider-model-id",), 1800.0, 900.0)
    common = dict(task_id="t", distro="Ubuntu", user="gbbbdemo", grok_path="/usr/bin/grok",
                  cwd="/tmp/gbbb-ask-0123456789abcdef", prompt="hello", max_turns=1,
                  timeout_sec=60, model=choice)
    plain = build_wsl_grok_ask_plan(**common)
    native = build_wsl_grok_ask_plan(**common, native_structured=(NATIVE_SYSTEM_PROMPT, schema))
    assert "--json-schema" not in plain.argv and "--verbatim" not in plain.argv
    argv = native.argv
    assert argv[argv.index("--system-prompt-override") + 1] == NATIVE_SYSTEM_PROMPT
    loaded = json.loads(argv[argv.index("--json-schema") + 1])
    assert loaded["additionalProperties"] is False
    assert loaded["required"] == ["files", "schema"] or loaded["required"] == ["schema", "files"]
    item = loaded["properties"]["files"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["path"]["enum"] == ["calc.py"]
    assert item["properties"]["content"]["type"] == "string"
    assert "--rules" not in argv and "--prompt-file" not in argv and "--prompt-json" not in argv
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert argv.count("--deny") == 7 and "MCPTool" in argv
    local = PL.build_local_plan(grok_path="/usr/bin/grok", cwd="/tmp/gbbb-ask-0123456789abcdef",
                                 prompt="hello", model=choice, max_turns=1, timeout_sec=60,
                                 native_structured=(NATIVE_SYSTEM_PROMPT, schema))
    assert local.argv == native.argv[native.argv.index("--exec") + 1:]
    assert "--json-schema" not in PL.build_local_plan(
        grok_path="/usr/bin/grok", cwd="/tmp/gbbb-ask-0123456789abcdef", prompt="hello",
        model=choice, max_turns=1, timeout_sec=60).argv


def test_parser_does_not_treat_snake_case_as_structured_text():
    snake = json.dumps(_base_result(structured_output=_value()))
    assert parse_grok_json_result(snake).parse_status == "no_text"
    allowed = parse_grok_json_result(
        json.dumps(_base_result(structuredOutput=_value())), allow_structured_without_text=True)
    assert allowed.parse_status == "ok" and allowed.model_ok is True
    still = parse_grok_json_result(json.dumps(_base_result(structured_output=_value())),
                                   allow_structured_without_text=True)
    assert still.parse_status == "no_text"


def test_native_accepts_structured_object_and_ignores_text(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path)
    before = path.read_bytes()
    dummy, calls, argv_path = _dummy(tmp_path, _base_result(
        text=CHATTER, structuredOutput=_value()))
    code, dry = _run(_body(dummy, src), capsys)
    assert code == 0 and dry["replacement_format"] == "native-schema-v1" and not calls.exists()
    sent = _body(dummy, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"])
    code, done = _run(sent, capsys)
    assert code == 0 and done["status"] == "proposal_accepted", done
    assert done["proposal"]["format"] == "native-schema-v1" and done["proposal"]["changed"] is True
    assert done["proposal"]["applied"] is False and done["source_writes"] is False
    assert path.read_bytes() == before and calls.read_text() == "1"
    assert CHATTER not in json.dumps(done) and "structured_state" not in done["result"]
    argv = json.loads(argv_path.read_text())
    assert argv[argv.index("--json-schema") + 1] == native_replacement_schema(["calc.py"])


def test_native_accepts_when_text_field_is_absent(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path)
    before = path.read_bytes()
    dummy, calls, _argv = _dummy(tmp_path, _base_result(structuredOutput=_value()))
    code, dry = _run(_body(dummy, src), capsys)
    sent = _body(dummy, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"],
                 show_proposal=True)
    code, done = _run(sent, capsys)
    assert code == 0 and done["status"] == "proposal_accepted"
    diff = done["proposal"]["files"][0]["diff"]
    assert '"""sum."""' in diff and "return a + b" in diff and "return a - b" in diff
    assert path.read_bytes() == before and calls.read_text() == "1"


def test_text_json_without_structured_field_is_rejected(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path)
    before = path.read_bytes()
    dummy, calls, _argv = _dummy(tmp_path, _base_result(text=json.dumps(_value())))
    code, dry = _run(_body(dummy, src), capsys)
    sent = _body(dummy, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"])
    code, done = _run(sent, capsys)
    assert code == 3 and done["status"] == "proposal_rejected"
    assert done["proposal"]["error"] == "structured_output_absent"
    assert done["proposal"]["diagnostic"]["text_channel_ignored"] is True
    assert path.read_bytes() == before and calls.read_text() == "1"
    assert "return a + b" not in json.dumps(done)


@pytest.mark.parametrize("obj,code", [
    (_base_result(text=CHATTER, structuredOutputError="SECRETERR"), "structured_output_rejected"),
    (_base_result(text=CHATTER, structuredOutput=_value(), structuredOutputError="SECRETERR"),
     "structured_output_ambiguous"),
    (_base_result(text=CHATTER, structuredOutput="not-an-object"), "structured_output_invalid"),
    (_base_result(text=CHATTER, structuredOutput={"schema": "gbbb.file_replacements/v1",
                                                 "files": [{"path": "other.py", "content": "x\n"}]}),
     "proposal_wrong_path"),
    (_base_result(text=CHATTER, structuredOutput={"schema": "gbbb.file_replacements/v1", "extra": 1,
                                                 "files": [{"path": "calc.py", "content": PROPOSED}]}),
     "proposal_wrong_schema"),
])
def test_native_failures_do_not_fall_back_or_echo(monkeypatch, tmp_path, capsys, obj, code):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path)
    before = path.read_bytes()
    dummy, calls, _argv = _dummy(tmp_path, obj)
    _code, dry = _run(_body(dummy, src), capsys)
    sent = _body(dummy, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"])
    status, done = _run(sent, capsys)
    blob = json.dumps(done)
    assert status == 3 and done["proposal"]["error"] == code
    assert path.read_bytes() == before and calls.read_text() == "1"
    assert "SECRETERR" not in blob and CHATTER not in blob and "return a + b" not in blob


def test_opt_in_rejected_reply_uses_structured_object_only(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, path = _tree(tmp_path)
    before = path.read_bytes()
    obj = _base_result(text=CHATTER, structuredOutput={"schema": "gbbb.file_replacements/v1", "extra": 1,
                                                      "files": [{"path": "calc.py", "content": PROPOSED}]})
    dummy, calls, _argv = _dummy(tmp_path, obj)
    _code, dry = _run(_body(dummy, src, show_rejected_reply=True, show_proposal=True), capsys)
    sent = _body(dummy, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"],
                 show_rejected_reply=True, show_proposal=True)
    status, done = _run(sent, capsys)
    assert status == 3 and done["proposal"]["error"] == "proposal_wrong_schema"
    assert "return a + b" in done["proposal"]["rejected_reply"] and CHATTER not in json.dumps(done)
    assert path.read_bytes() == before and calls.read_text() == "1"


def test_format_change_does_not_call_the_model(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, _path = _tree(tmp_path)
    dummy, calls, _argv = _dummy(tmp_path, _base_result(structuredOutput=_value()))
    _code, dry = _run(_body(dummy, src), capsys)
    sent = _body(dummy, src, execute=True, allow_model_call=True, confirm_send=dry["consent_token"],
                 replacement_format="json-v1")
    status, done = _run(sent, capsys)
    assert status == 2 and done["error"]["code"] == "consent_mismatch" and not calls.exists()


def test_envelope_cannot_carry_a_schema(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    src, _path = _tree(tmp_path)
    dummy, calls, _argv = _dummy(tmp_path, _base_result(structuredOutput=_value()))
    body = _body(dummy, src, json_schema={"type": "object"})
    status, done = _run(body, capsys)
    assert status == 2 and done["error"]["code"] == "schema_invalid" and not calls.exists()

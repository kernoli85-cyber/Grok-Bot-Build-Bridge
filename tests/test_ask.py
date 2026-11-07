"""`ask` workflow. WSL is MOCKED here (fake distro list, fake mktemp/find); the Grok
side is a real dummy subprocess emitting representative JSON. Not a real-host test."""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from grok_bot_build_bridge import cli
from grok_bot_build_bridge.runner import plan as P
from grok_bot_build_bridge.runner.grok_result import extract_answer_text
from grok_bot_build_bridge.safe_exec import run_argv
from grok_bot_build_bridge.state import JobStore

FIX = Path(__file__).parent / "fixtures"
DUMMY = FIX / "bridge-dummy-grokjson.py"
RUNNING = "  NAME      STATE           VERSION\n* Ubuntu    Running         2\n"
CWD = "/tmp/gbbb-ask-MockDir0001"
BASE = ["ask", "--wsl-distro", "Ubuntu", "--wsl-user", "gbbbdemo", "--executable", "/usr/bin/grok"]


def _mock_wsl(monkeypatch, *, verify_ok=True, mk=CWD):
    seen = []

    def runner(argv):
        seen.append(argv)
        if "/usr/bin/mktemp" in argv:
            return 0, mk + "\n"
        if "/usr/bin/find" in argv:
            return (0, argv[argv.index("/usr/bin/find") + 1] + "\n") if verify_ok else (0, "")
        raise AssertionError("unexpected wsl argv")
    monkeypatch.setattr(cli, "_wsl_list_reader", lambda: (RUNNING, None))
    monkeypatch.setattr(cli, "_wsl_cmd_runner", runner)
    return seen


def _real_dummy(monkeypatch, mode, captured=None, hook=None):
    """Replace the wsl.exe spawn with a REAL local dummy process (mock transport)."""
    import grok_bot_build_bridge.runner.worker as w
    orig = w.execute_one_shot

    def ex(argv, **kw):
        if captured is not None:
            captured.append(tuple(argv))
        if hook:
            hook()
        return run_argv([sys.executable, str(DUMMY), mode], timeout_sec=30)
    monkeypatch.setattr(cli, "execute_one_shot", lambda s, p, **k: orig(s, p, executor=ex, **k))


def test_dry_run_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_wsl_list_reader", lambda: pytest.fail("no host call in dry run"))
    monkeypatch.setattr(cli, "_wsl_cmd_runner", lambda a: pytest.fail("no host call in dry run"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    db = tmp_path / "s.db"
    secret = 'Sag "Grüße" 🙂 SECRETPROMPT'
    assert cli.main(BASE + ["--prompt", secret, "--state-db", str(db)]) == 0
    out = capsys.readouterr().out
    d = json.loads(out)
    assert d["dry_run"] and not d["state_written"] and not d["temp_dir_created"]
    assert "SECRETPROMPT" not in out and "<redacted_prompt>" in d["argv_preview"]
    assert "VISIBLE" in d["limits"]["prompt_process_visibility"]
    assert not db.exists() and not (tmp_path / "data").exists()


@pytest.mark.parametrize("bad,code", [
    ("", "prompt_empty"), ("--yolo do it", "prompt_flag_like"), ("-p x", "prompt_flag_like"),
    ("a\x00b", "prompt_control_chars"), ("a\x1b[2Jb", "prompt_control_chars"),
    ("x\u202eevil", "prompt_control_chars"), ("x" * (P.ASK_MAX_PROMPT_BYTES + 1), "prompt_too_large"),
])
def test_prompt_validation(bad, code):
    with pytest.raises(P.PlanError) as e:
        P.validate_ask_prompt(bad)
    assert e.value.code == code


def test_prompt_bytes_bad_utf8_and_multibyte_bound():
    with pytest.raises(P.PlanError) as e:
        P.validate_ask_prompt(b"\xff\xfe")
    assert e.value.code == "prompt_not_utf8"
    ok = "ü" * (P.ASK_MAX_PROMPT_BYTES // 2)
    assert P.validate_ask_prompt(ok) == ok
    with pytest.raises(P.PlanError):
        P.validate_ask_prompt(ok + "ü")


def test_prompt_mentioning_flags_is_a_single_value():
    pl = P.build_wsl_grok_ask_plan(task_id="t1", distro="Ubuntu", user="gbbbdemo", grok_path="/usr/bin/grok",
                                   cwd=CWD, prompt='Explain "--yolo; rm -rf /" and $(id)', max_turns=3)
    i = pl.argv.index("--single")
    assert pl.argv[i + 1] == 'Explain "--yolo; rm -rf /" and $(id)'
    assert pl.argv.count("--single") == 1 and pl.argv[pl.argv.index("--max-turns") + 1] == "3"
    assert pl.expected_text is None and pl.parse_grok_json
    assert "bypassPermissions" not in pl.argv and "--always-approve" not in pl.argv


@pytest.mark.parametrize("kw,code", [
    (dict(cwd="/home/gbbbdemo"), "ask_cwd_invalid"), (dict(cwd="/tmp/gbbb-ask-../x"), "ask_cwd_invalid"),
    (dict(cwd="/tmp/gbbb-smoke-abcdefgh"), "ask_cwd_invalid"), (dict(max_turns=0), "max_turns_invalid"),
    (dict(max_turns=11), "max_turns_invalid"), (dict(timeout_sec=601), "timeout_invalid"),
    (dict(user="root"), "wsl_root_refused"), (dict(grok_path="grok"), "grok_path_invalid"),
])
def test_plan_rejects(kw, code):
    base = dict(task_id="t", distro="Ubuntu", user="gbbbdemo", grok_path="/usr/bin/grok", cwd=CWD, prompt="hi")
    base.update(kw)
    with pytest.raises(P.PlanError) as e:
        P.build_wsl_grok_ask_plan(**base)
    assert e.value.code == code


def test_execute_needs_allow_model_call(tmp_path, capsys, monkeypatch):
    seen = _mock_wsl(monkeypatch)
    assert cli.main(BASE + ["--prompt", "hi", "--execute", "--state-db", str(tmp_path / "s.db")]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "model_call_gated"
    assert not seen and not (tmp_path / "s.db").exists()


def test_generic_answer_success_show_answer_transient(tmp_path, capsys, monkeypatch):
    seen = _mock_wsl(monkeypatch)
    cap = []
    _real_dummy(monkeypatch, "answer", cap)
    db = tmp_path / "s.db"
    rc = cli.main(BASE + ["--prompt", 'Was ist "2+2"? Grüße', "--execute", "--allow-model-call",
                          "--state-db", str(db), "--show-answer", "--max-turns", "2"])
    d = json.loads(capsys.readouterr().out)
    assert rc == 0 and d["status"] == "succeeded" and d["cwd"] == CWD
    assert d["answer"].startswith("Antwort: 2+2 = 4.\nZitat: \"ok\"") and "\x1b" not in d["answer"]
    assert "Grüße" in d["answer"] and d["result_summary"]["matched_text"] is None
    assert d["result_summary"]["total_cost_usd"] == "0.00012345"
    assert cap[0][0] == "wsl.exe" and cap[0][cap[0].index("--cd") + 1] == CWD
    assert any("/usr/bin/mktemp" in a for a in seen) and any("/usr/bin/find" in a for a in seen)
    # Nothing of the answer or prompt persisted anywhere in the DB file.
    raw = db.read_bytes()
    assert b"Antwort" not in raw and b"2+2" not in raw and "Grüße".encode() not in raw
    st = JobStore(db)
    blob = json.dumps([str(e) for e in st.list_outbox()])
    assert "sess-ANS-1" not in blob


def test_no_show_answer_hides_text(tmp_path, capsys, monkeypatch):
    _mock_wsl(monkeypatch)
    _real_dummy(monkeypatch, "answer")
    assert cli.main(BASE + ["--prompt", "hi", "--execute", "--allow-model-call",
                            "--state-db", str(tmp_path / "s.db")]) == 0
    out = capsys.readouterr().out
    assert "Antwort" not in out and "answer" not in json.loads(out)


@pytest.mark.parametrize("mode,code", [("noise", "success_exit_invalid_result"),
                                       ("error", "model_result_not_ok"),
                                       ("badutf8", "success_exit_invalid_result")])
def test_invalid_outputs_need_review(tmp_path, capsys, monkeypatch, mode, code):
    _mock_wsl(monkeypatch)
    _real_dummy(monkeypatch, mode)
    rc = cli.main(BASE + ["--prompt", "hi", "--execute", "--allow-model-call",
                          "--state-db", str(tmp_path / "s.db"), "--show-answer"])
    d = json.loads(capsys.readouterr().out)
    assert rc == 1 and d["status"] == "needs_review" and d["error_code"] == code


def test_unsafe_cwd_refused_before_model(tmp_path, capsys, monkeypatch):
    _mock_wsl(monkeypatch, verify_ok=False)
    _real_dummy(monkeypatch, "answer", hook=lambda: pytest.fail("model must not run"))
    assert cli.main(BASE + ["--prompt", "hi", "--execute", "--allow-model-call",
                            "--state-db", str(tmp_path / "s.db")]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "ask_cwd_unsafe"
    assert not (tmp_path / "s.db").exists()


def test_mktemp_output_injection_refused(tmp_path, capsys, monkeypatch):
    _mock_wsl(monkeypatch, mk="/home/gbbbdemo")
    assert cli.main(BASE + ["--prompt", "hi", "--execute", "--allow-model-call",
                            "--state-db", str(tmp_path / "s.db")]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "ask_cwd_invalid"


def test_verify_argv_is_fixed_read_only():
    a = P.build_wsl_verify_ask_cwd_argv("Ubuntu", "gbbbdemo", CWD)
    assert a[a.index("--exec") + 1] == "/usr/bin/find" and "-delete" not in a and "-exec" not in a
    assert "-type" in a and "-empty" in a and "/022" in a


def test_cancel_and_persist_failure_hide_answer(tmp_path, capsys, monkeypatch):
    _mock_wsl(monkeypatch)
    db = tmp_path / "s.db"
    st_holder = {}
    orig_store = cli._store_from_args

    def store(args):
        st_holder["s"] = orig_store(args)
        return st_holder["s"]
    monkeypatch.setattr(cli, "_store_from_args", store)
    _real_dummy(monkeypatch, "answer", hook=lambda: st_holder["s"].cancel(st_holder["tid"]))
    st_holder["tid"] = "ask-cancel-1"
    rc = cli.main(BASE + ["--prompt", "hi", "--execute", "--allow-model-call", "--task-id", "ask-cancel-1",
                          "--state-db", str(db), "--show-answer"])
    d = json.loads(capsys.readouterr().out)
    assert rc == 1 and d["status"] == "cancelled" and d["answer"] is None and d["result_summary"] is None


def test_timeout_needs_review_no_retry(tmp_path, capsys, monkeypatch):
    from grok_bot_build_bridge.safe_exec import RunResult
    import grok_bot_build_bridge.runner.worker as w
    _mock_wsl(monkeypatch)
    calls = []
    orig = w.execute_one_shot

    def ex(argv, **kw):
        calls.append(1)
        return RunResult(tuple(argv), None, "", "", timed_out=True, error="timeout")
    monkeypatch.setattr(cli, "execute_one_shot", lambda s, p, **k: orig(s, p, executor=ex, **k))
    rc = cli.main(BASE + ["--prompt", "hi", "--execute", "--allow-model-call", "--state-db", str(tmp_path / "s.db")])
    d = json.loads(capsys.readouterr().out)
    assert rc == 1 and d["status"] == "needs_review" and d["error_code"] == "timeout_uncertain" and calls == [1]


def test_extract_answer_bounds():
    big = json.dumps({"text": "a" * 20000, "stopReason": "end_turn"})
    t, cut = extract_answer_text(big)
    assert cut and len(t) == 16000
    assert extract_answer_text("noise " + big) == (None, False)


def test_prompt_file_and_stdin(tmp_path, capsys, monkeypatch):
    f = tmp_path / "p.txt"
    f.write_bytes("Zeile 1\nZeile \"2\" ü\n".encode())
    assert cli.main(BASE + ["--prompt-file", str(f)]) == 0
    assert json.loads(capsys.readouterr().out)["limits"]["prompt_bytes"] == len("Zeile 1\nZeile \"2\" ü".encode())
    assert cli.main(BASE + ["--prompt", "a", "--prompt-file", str(f)]) == 2
    capsys.readouterr()
    import io
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\xff")))
    assert cli.main(BASE + ["--prompt-stdin"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "prompt_not_utf8"

"""Direct POSIX runner + ask-context execute against a DUMMY grok (no real grok, no model)."""
import json
import os
import subprocess
import sys

import pytest

if os.name != "posix" or os.getuid() == 0:
    pytest.skip("POSIX non-root only", allow_module_level=True)

from grok_bot_build_bridge.runner import posix_local as PL
from test_ask_context_cli import _run, setup  # noqa: F401  (fixture reuse)

DUMMY = r'''#!{py}
import json, os, sys, time
a = sys.argv[1:]
need = ["--sandbox", "strict", "--permission-mode", "dontAsk", "--max-turns", "--disable-web-search",
        "--no-subagents", "--no-plan", "--deny", "MCPTool", "--model", "LM-Studio", "--single"]
dis = a[a.index("--disallowed-tools") + 1] if "--disallowed-tools" in a else ""
cwd = os.getcwd()
ok = (all(n in a for n in need) and all(t in dis for t in ("use_tool", "read_file", "write", "image_gen"))
      and cwd.startswith("/tmp/gbbb-ask-") and os.listdir(cwd) == [] and a[a.index("--cwd") + 1] == cwd
      and os.environ.get("GROK_MEMORY") == "0" and os.environ.get("GROK_TITLE_REFRESH") == "false"
      and "--always-approve" not in a and "SECRET_PARENT" not in os.environ)
if not ok:
    sys.stderr.write("protection missing"); sys.exit(9)
mode = open(os.path.join(os.path.dirname(__file__), "mode")).read().strip()
if mode == "sleep":
    time.sleep(60)
if mode == "flood":
    sys.stdout.write("x" * 200000); sys.exit(0)
if mode == "litter":
    open("left.txt", "w").write("x")
if mode == "child":
    import subprocess
    subprocess.Popen(["/bin/sleep", "30"])
key = "other-model" if mode == "mismatch" else "qwen3.5-9b-mtp"
print(json.dumps({{"text": "47113", "stopReason": "end_turn", "sessionId": "sess-SECRET-1", "num_turns": 1,
                  "usage": {{"input_tokens": 5, "output_tokens": 1}}, "modelUsage": {{key: {{"inputTokens": 5}}}}}}))
'''


@pytest.fixture
def dummy(setup):  # noqa: F811
    b = setup / "bin"
    b.mkdir(mode=0o755)
    exe = b / "grok"
    exe.write_text(DUMMY.format(py=sys.executable))
    exe.chmod(0o755)
    return setup, exe


def _exec(setup, exe, mode, *extra, **kw):
    (exe.parent / "mode").write_text(mode)
    _, d = _run(setup, *extra, exe=exe, **kw)
    before = set(os.listdir("/tmp"))
    env_secret = os.environ.get("SECRET_PARENT")
    os.environ["SECRET_PARENT"] = "1"
    try:
        rc, r = _run(setup, *extra, "--execute", "--allow-model-call", "--confirm-send", d["consent_token"], exe=exe, **kw)
    finally:
        if env_secret is None:
            os.environ.pop("SECRET_PARENT")
    new = {n for n in set(os.listdir("/tmp")) - before if n.startswith("gbbb-ask-")}
    return rc, r, new


def test_success_identity_confirmed_nothing_leaked(dummy):
    setup, exe = dummy
    rc, r, new = _exec(setup, exe, "ok")
    res = r["result"]
    assert rc == 0 and res["status"] == "succeeded" and res["model_identity"] == "confirmed", r
    assert res["cwd_cleanup"] == "removed" and not new and res["process_cleanup"] == "group_empty"
    assert res["cost_status"] == "unknown" and res["total_cost_usd"] is None and res["retry"] is False
    blob = json.dumps(r)
    assert "sess-SECRET" not in blob and "47113" not in blob and str(setup) not in blob


def test_show_answer_is_transient_opt_in(dummy):
    setup, exe = dummy
    rc, r, _ = _exec(setup, exe, "ok", "--show-answer")
    assert r["result"]["answer"] == "47113"


@pytest.mark.parametrize("mode,code", [("mismatch", "model_identity_mismatch"), ("flood", "output_truncated")])
def test_needs_review_cases(dummy, mode, code):
    setup, exe = dummy
    rc, r, new = _exec(setup, exe, mode)
    assert rc == 3 and r["result"]["status"] == "needs_review" and r["result"]["error_code"] == code and not new


def test_timeout_kills_and_cleans(dummy):
    setup, exe = dummy
    rc, r, new = _exec(setup, exe, "sleep", "--timeout", "1")
    res = r["result"]
    assert res["status"] == "needs_review" and res["error_code"] == "timeout" and not new


def test_lingering_child_is_killed_and_reported(dummy):
    setup, exe = dummy
    rc, r, new = _exec(setup, exe, "child")
    res = r["result"]
    assert res["process_cleanup"] == "group_killed" and res["status"] == "needs_review"
    assert res["error_code"] == "cleanup_unproven" and not new


def test_cwd_litter_left_not_deleted_recursively(dummy):
    setup, exe = dummy
    rc, r, new = _exec(setup, exe, "litter")
    assert r["result"]["cwd_cleanup"] == "left_nonempty" and len(new) == 1
    d = "/tmp/" + new.pop()
    os.unlink(d + "/left.txt")
    os.rmdir(d)


def test_stale_consent_after_source_edit_never_runs(dummy):
    setup, exe = dummy
    (exe.parent / "mode").write_text("ok")
    _, d = _run(setup, exe=exe)
    (setup / "src/docs/notes.txt").write_text("changed\n")
    marker = exe.parent / "ran"
    exe.write_text(exe.read_text().replace("mode = open", f"open({str(marker)!r}, 'w').close()\nmode = open"))
    rc, e = _run(setup, "--execute", "--allow-model-call", "--confirm-send", d["consent_token"], exe=exe)
    assert e["error"]["code"] == "consent_mismatch" and not marker.exists()


@pytest.mark.parametrize("perm", [0o777, 0o775])
def test_untrusted_executable_refused(dummy, perm):
    setup, exe = dummy
    _, d = _run(setup, exe=exe)
    exe.chmod(perm)
    rc, e = _run(setup, "--execute", "--allow-model-call", "--confirm-send", d["consent_token"], exe=exe)
    assert e["error"]["code"] == "executable_untrusted"


def test_relative_executable_refused():
    with pytest.raises(PL.RunnerError) as e:
        PL.validate_executable("grok")
    assert e.value.code == "executable_invalid"


def test_local_argv_is_exact_guest_part_of_wsl_plan():
    from grok_bot_build_bridge.model_select import ModelChoice
    from grok_bot_build_bridge.runner.plan import build_wsl_grok_ask_plan
    m = ModelChoice("LM-Studio", "per_job", "custom_local", ("k",), 1800.0, 900.0)
    cwd = "/tmp/gbbb-ask-0123456789abcdef"
    lp = PL.build_local_plan(grok_path="/opt/grok", cwd=cwd, prompt="hi", model=m, max_turns=1, timeout_sec=60)
    w = build_wsl_grok_ask_plan(task_id="x", distro="Ubuntu", user="u", grok_path="/opt/grok", cwd=cwd,
                                prompt="hi", timeout_sec=60, model=m)
    assert lp.argv == w.argv[w.argv.index("--exec") + 1:] and "wsl.exe" not in lp.argv

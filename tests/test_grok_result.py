import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from grok_bot_build_bridge import cli
from grok_bot_build_bridge.runner import build_approved_plan, execute_one_shot
from grok_bot_build_bridge.runner.grok_result import (
    MAX_RESULT_BYTES, classify, parse_grok_json_result,
)
from grok_bot_build_bridge.safe_exec import RunResult
from grok_bot_build_bridge.state import JobStore

FIX = Path(__file__).parent / "fixtures"
DOCS = (FIX / "grok_json_result_docs_example.json").read_text()


def test_docs_example_parses_allowlisted_fields_only():
    s = parse_grok_json_result(DOCS)
    assert s.parse_status == "ok" and s.model_ok and s.stop_reason == "end_turn"
    assert s.session_id == "abc123" and s.num_turns == 7
    assert s.total_cost_usd == 0.01268905 and s.total_cost_usd_ticks == 126890500
    assert s.cost_status == "reported" and s.usage["total_tokens"] == 50103
    d = s.to_dict()
    assert "summary of the codebase" not in json.dumps(d)  # text never echoed
    assert s.expected_match is None and s.matched_text is None


@pytest.mark.parametrize("raw,status", [
    (None, "empty"), ("", "empty"), ("not json", "not_json"), ("[1]", "not_object"),
    ('{"stopReason":"end_turn"}', "no_text"), ('{"text": 5}', "no_text"),
    ('log\n{"text":"BRIDGE_OK","stopReason":"end_turn"}', "not_json"),
    ('{"text":"\ufffd","stopReason":"end_turn"}', "not_utf8"),
    ("x" * (MAX_RESULT_BYTES + 1), "oversize"),
    ("[" * 100000 + "]" * 100000, "not_object"),
])
def test_invalid_results_never_ok(raw, status):
    s = parse_grok_json_result(raw, expected_text="BRIDGE_OK")
    assert s.parse_status in {status, "not_json", "oversize"}
    assert not s.model_ok and s.matched_text is None
    assert classify(s, require_expected=True) == "success_exit_invalid_result"


def test_truncated_and_bad_bytes():
    assert parse_grok_json_result('{"text":"BRIDGE_OK"}', truncated=True).parse_status == "truncated"
    assert parse_grok_json_result(b'{"text":"\xff"}').parse_status == "not_utf8"


@pytest.mark.parametrize("cost", ["NaN", "Infinity", "-1", '"0.1"', "true", "1e30"])
def test_bad_cost_is_unknown(cost):
    s = parse_grok_json_result('{"text":"BRIDGE_OK","stopReason":"end_turn","total_cost_usd":%s}' % cost)
    assert s.total_cost_usd is None and s.cost_status == "unknown"


def test_partial_cost_omitted_and_absent_unknown():
    s = parse_grok_json_result('{"text":"a","stopReason":"end_turn","cost_is_partial":true,"total_cost_usd":1}')
    assert s.cost_status == "partial_omitted" and s.total_cost_usd is None
    s = parse_grok_json_result('{"text":"a","stopReason":"end_turn"}')
    assert s.cost_status == "unknown" and s.total_cost_usd is None


def test_session_and_stop_allowlist():
    s = parse_grok_json_result('{"text":"a","stopReason":"END; rm","sessionId":"../../etc/passwd"}')
    assert s.session_id is None and s.stop_reason is None and not s.model_ok
    assert classify(s, require_expected=False) == "model_result_not_ok"


def test_exact_match_not_substring():
    ok = parse_grok_json_result('{"text":" BRIDGE_OK\\n","stopReason":"end_turn"}', expected_text="BRIDGE_OK")
    bad = parse_grok_json_result('{"text":"Sure! BRIDGE_OK","stopReason":"end_turn"}', expected_text="BRIDGE_OK")
    assert ok.expected_match and ok.matched_text == "BRIDGE_OK"
    assert bad.expected_match is False and bad.matched_text is None
    assert classify(bad, require_expected=True) == "smoke_text_mismatch"
    err = parse_grok_json_result('{"text":"BRIDGE_OK","stopReason":"end_turn","is_error":true}', expected_text="BRIDGE_OK")
    assert classify(err, require_expected=True) == "model_result_not_ok"


def _dummy_plan(mode, tid):
    p = build_approved_plan(task_id=tid, backend="native" if False else "wsl", cwd="/tmp",
                            executable=str(FIX / "bridge-dummy-grokjson.py"), extra_args=(),
                            prompt=None)
    # dummy ignores --cwd tokens; pass mode as the first argv after script
    argv = list(p.argv)
    i = argv.index(str(FIX / "bridge-dummy-grokjson.py"))
    argv = argv[: i + 1] + [mode]
    return replace(p, argv=tuple(argv), parse_grok_json=True, expected_text="BRIDGE_OK")


@pytest.mark.parametrize("mode,status,code", [
    ("ok", "succeeded", None),
    ("noise", "needs_review", "success_exit_invalid_result"),
    ("mismatch", "needs_review", "smoke_text_mismatch"),
    ("badutf8", "needs_review", "success_exit_invalid_result"),
])
def test_real_dummy_process_paths(tmp_path, mode, status, code):
    store = JobStore(tmp_path / "s.db")
    out = execute_one_shot(store, _dummy_plan(mode, f"t-{mode}"), owner="w", dry_run=False)
    assert out.returncode == 0 and out.status == status and out.error_code == code
    assert store.get_job(f"t-{mode}").status.value == status
    saved = store.get_job_result(f"t-{mode}")
    assert saved is not None and saved == out.result_summary
    blob = json.dumps([e.payload_json if hasattr(e, "payload_json") else str(e) for e in store.list_outbox()])
    assert "BRIDGE_OK" not in blob and "0d3f-AB_9" not in blob  # never in outbox
    if mode == "ok":
        assert saved["matched_text"] == "BRIDGE_OK" and saved["cost_status"] == "reported"
        assert saved["session_id"] == "0d3f-AB_9"
    # needs_review is terminal for automation: no auto requeue
    assert store.get_job(f"t-{mode}").attempt <= 1


def test_nonzero_exit_stays_failed_with_result_recorded(tmp_path):
    store = JobStore(tmp_path / "s.db")
    plan = replace(_dummy_plan("ok", "t-x"), parse_grok_json=True)

    def fake(argv, **kw):
        return RunResult(tuple(argv), 2, '{"text":"x","stopReason":"end_turn"}', "")
    out = execute_one_shot(store, plan, owner="w", dry_run=False, executor=fake)
    assert out.status == "failed" and out.error_code == "exit_2"


def test_status_hides_result_by_default(tmp_path, capsys):
    db = tmp_path / "s.db"
    execute_one_shot(JobStore(db), _dummy_plan("ok", "t-h"), owner="w", dry_run=False)
    assert cli.main(["jobs-status", "--state-db", str(db)]) == 0
    assert "result_summary" not in capsys.readouterr().out
    assert cli.main(["jobs-status", "--state-db", str(db), "--show-result"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"][0]["result_summary"]["matched_text"] == "BRIDGE_OK"


def test_migration_on_pre_v0163_db(tmp_path):
    db = tmp_path / "old.db"
    JobStore(db)
    with sqlite3.connect(db) as c:
        c.execute("DROP TABLE job_results")
    s = JobStore(db)  # idempotent re-init adds table
    s.enqueue_job(task_id="m1", backend="wsl", cwd="/tmp")
    s.put_job_result("m1", {"parse_status": "ok"})
    s.put_job_result("m1", {"parse_status": "overwrite"})  # first write wins
    assert JobStore(db).get_job_result("m1") == {"parse_status": "ok"}

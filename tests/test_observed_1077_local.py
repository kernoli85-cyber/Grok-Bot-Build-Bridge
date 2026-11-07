"""Regression tests from ONE observed grok 1.0.77 headless json result (-m LM-Studio, local
LM Studio, 2026-11-07). The fixture keeps the observed field names, nesting and numbers;
session/request ids, cwd and thought text are REPLACED placeholders (not a raw capture)."""
import json
from pathlib import Path

import pytest

from grok_bot_build_bridge import cli
from grok_bot_build_bridge import model_select as M
from grok_bot_build_bridge.runner import plan as P
from grok_bot_build_bridge.runner.grok_result import (classify, extract_answer_text,
                                                      parse_grok_json_result, validate_summary)

FX = Path(__file__).parent / "fixtures" / "grok_json_1077_local_observed_redacted.json"


def _raw(drop_note=True, **edit) -> str:
    d = json.loads(FX.read_text())
    if drop_note:
        d.pop("_fixture_note")
    d.update(edit)
    return json.dumps(d, indent=2)


EXPECT = ("LM-Studio", "qwen3.5-9b-mtp")


def test_observed_shape_parses_confirmed_with_explicit_key():
    s = parse_grok_json_result(_raw(), expected_models=EXPECT)
    assert s.parse_status == "ok" and s.model_ok and s.stop_reason == "end_turn"
    assert s.is_error is None  # field absent in the real output: unknown, not an error
    assert s.num_turns == 1 and s.model_identity == "confirmed"
    assert s.usage == {"input_tokens": 9299, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                       "output_tokens": 29, "reasoning_tokens": 21, "total_tokens": 9328}
    # Stored as reported; reasoning is part of output here (9299 + 29 == 9328), never re-added.
    assert s.usage["input_tokens"] + s.usage["output_tokens"] == s.usage["total_tokens"]
    assert s.cost_status == "unknown" and s.total_cost_usd is None  # absent cost != free
    assert classify(s, require_expected=False) is None
    d = s.to_dict()
    assert validate_summary(d)
    blob = json.dumps(d)
    for leak in ("qwen", "LM-Studio", "placeholder reasoning", "/tmp/gbbb-ask", "00000000-0000-0000-0000-000000000002"):
        assert leak not in blob
    assert extract_answer_text(_raw())[0] == "LOCAL_BRIDGE_OK"


def test_alias_without_learned_key_is_mismatch_not_success():
    s = parse_grok_json_result(_raw(), expected_models=("LM-Studio",))
    assert s.model_identity == "mismatch" and classify(s, require_expected=False) == "model_identity_mismatch"


@pytest.mark.parametrize("edit,ident", [
    ({"modelUsage": {"qwen3.5-9b-mtp": {}, "grok-4.6": {}}}, "mismatch"),
    ({"modelUsage": {}}, "unreported"),
    ({"modelUsage": None}, "unreported"),
    ({"modelUsage": {"QWEN3.5-9B-MTP": {}}}, "mismatch"),  # case-sensitive
])
def test_multi_missing_and_case(edit, ident):
    assert parse_grok_json_result(_raw(**edit), expected_models=EXPECT).model_identity == ident


@pytest.mark.parametrize("edit,code", [
    ({"stopReason": "max_turn_requests"}, "model_result_not_ok"),
    ({"is_error": True}, "model_result_not_ok"),
])
def test_success_still_needs_end_turn_and_no_error(edit, code):
    assert classify(parse_grok_json_result(_raw(**edit), expected_models=EXPECT), require_expected=False) == code


def test_exit_zero_alone_is_not_success(tmp_path, capsys, monkeypatch):
    from test_ask import BASE, _mock_wsl
    import grok_bot_build_bridge.runner.worker as w
    from grok_bot_build_bridge.safe_exec import RunResult
    _mock_wsl(monkeypatch)
    orig = w.execute_one_shot

    def ex(argv, **kw):
        return RunResult(argv=tuple(argv), returncode=0, stdout="not json", stderr="", timed_out=False,
                         stdout_truncated=False, stderr_truncated=False, error=None)
    monkeypatch.setattr(cli, "execute_one_shot", lambda s, p, **k: orig(s, p, executor=ex, **k))
    rc = cli.main(BASE + ["--prompt", "x", "--model", "LM-Studio", "--execute", "--allow-model-call",
                          "--state-db", str(tmp_path / "s.db")])
    d = json.loads(capsys.readouterr().out)
    assert rc != 0 and d["status"] == "needs_review"


def test_ask_plan_guest_timeout_and_documented_off_switches():
    c = M.resolve_model("LM-Studio", M.ModelSettings(None, {"LM-Studio": M.ModelAlias("LM-Studio", "custom_local", ("qwen3.5-9b-mtp",))}))
    p = P.build_wsl_grok_ask_plan(task_id="t", distro="Ubuntu", user="okern", grok_path="/home/okern/.local/bin/grok",
                                  cwd="/tmp/gbbb-ask-MockDir0001", prompt="Antworte nur LOCAL_BRIDGE_OK",
                                  timeout_sec=900, model=c)
    a = p.argv
    i = a.index("--exec")
    assert a[i + 1:i + 6] == ("/usr/bin/timeout", "-k", "10", "900", "/usr/bin/env")
    for e in P.ASK_EXTRA_ENV:
        assert e in a and a.index(e) < a.index("/home/okern/.local/bin/grok")
    assert a[a.index("--model") + 1] == "LM-Studio" and p.timeout_sec == 930.0
    assert p.expected_models == EXPECT
    for bad in ("--always-approve", "--yolo", "bypassPermissions"):
        assert bad not in a


def test_check_result_onboarding_explicit_only(tmp_path, capsys):
    sp, rf = str(tmp_path / "ms.json"), tmp_path / "r.json"
    rf.write_text(_raw())
    assert cli.main(["model-settings", "--settings", sp, "add-alias", "LM-Studio"]) == 0
    capsys.readouterr()
    rc = cli.main(["model-settings", "--settings", sp, "check-result", "LM-Studio", "--result-file", str(rf)])
    d = json.loads(capsys.readouterr().out)
    assert rc == 3 and d["identity_with_current_settings"] == "mismatch" and d["saved"] is False
    assert d["reported_model_usage_keys"] == ["qwen3.5-9b-mtp"] and "--set-usage-key qwen3.5-9b-mtp" in d["to_accept_run"]
    assert "LOCAL_BRIDGE_OK" not in json.dumps(d) and "placeholder" not in json.dumps(d)
    assert M.load_settings(Path(sp)).aliases["LM-Studio"].expect_usage_keys == ()  # nothing persisted
    assert cli.main(["model-settings", "--settings", sp, "check-result", "LM-Studio", "--result-file", str(rf),
                     "--set-usage-key", "grok-4.6"]) == 2  # must be an observed key
    capsys.readouterr()
    assert cli.main(["model-settings", "--settings", sp, "check-result", "LM-Studio", "--result-file", str(rf),
                     "--set-usage-key", "qwen3.5-9b-mtp"]) == 0
    assert M.load_settings(Path(sp)).aliases["LM-Studio"].expect_usage_keys == ("qwen3.5-9b-mtp",)
    capsys.readouterr()
    assert cli.main(["model-settings", "--settings", sp, "check-result", "LM-Studio", "--result-file", str(rf)]) == 0
    # Owner switched the model loaded in LM Studio: reported id changes -> mismatch until explicit update.
    rf.write_text(_raw(modelUsage={"other-local-model": {"modelCalls": 1}}))
    capsys.readouterr()
    assert cli.main(["model-settings", "--settings", sp, "check-result", "LM-Studio", "--result-file", str(rf)]) == 3
    assert M.load_settings(Path(sp)).aliases["LM-Studio"].expect_usage_keys == ("qwen3.5-9b-mtp",)
    rf.write_text(_raw(modelUsage={"a-model": {}, "b-model": {}}))
    assert cli.main(["model-settings", "--settings", sp, "check-result", "LM-Studio", "--result-file", str(rf),
                     "--set-usage-key", "a-model"]) == 2


@pytest.mark.parametrize("raw,code", [
    (b'{"modelUsage": {}}', "model_usage_absent"), (b'{"text": "x"}', "model_usage_absent"),
    (b'{"modelUsage": {"a b": {}}}', "model_usage_invalid"), (b"\xff", "result_invalid"),
    (b'{"modelUsage": {"a": {}}, "modelUsage": {"b": {}}}', "result_invalid"), (b"x" * 70000, "result_too_large"),
])
def test_usage_keys_reader_bounds(raw, code):
    with pytest.raises(M.ModelSelectionError) as e:
        M.usage_keys_from_result(raw)
    assert e.value.code == code

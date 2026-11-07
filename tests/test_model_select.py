"""Owner model selection: settings, per-job binding, argv safety, identity check. Mock/fixture
only: WSL mocked, Grok replaced by a local dummy. No model, no LM Studio, no `grok models`."""
import json
import os
import stat
import sys

import pytest

from grok_bot_build_bridge import cli
from grok_bot_build_bridge import model_select as M
from grok_bot_build_bridge.runner import plan as P
from grok_bot_build_bridge.runner.grok_result import (SUMMARY_SCHEMA_V1, classify,
                                                      parse_grok_json_result, validate_summary)
from test_ask import BASE, CWD, _mock_wsl, _real_dummy


@pytest.mark.parametrize("bad,code", [
    ("", "model_id_invalid"), ("-m", "model_id_invalid"), ("--yolo", "model_id_invalid"),
    ("a b", "model_id_invalid"), ("a\nb", "model_id_invalid"), ("x" * 65, "model_id_invalid"),
    ("grok\u202e4", "model_id_non_ascii"), ("ｇrok", "model_id_non_ascii"), ("lm\u00e9", "model_id_non_ascii"),
    ("a/../b", "model_id_invalid"), ("a;rm", "model_id_invalid"), ("$(x)", "model_id_invalid"), (7, "model_id_invalid"),
])
def test_model_id_validation(bad, code):
    with pytest.raises(M.ModelSelectionError) as e:
        M.validate_model_id(bad)
    assert e.value.code == code


@pytest.mark.parametrize("ok", ["grok-4.6", "LM-Studio", "lm-studio", "qwen2.5-coder:7b", "org/model@v1"])
def test_model_id_accepts_documented_shapes(ok):
    assert M.validate_model_id(ok) == ok


def _settings(tmp_path, **kw):
    p = tmp_path / "cfg" / "model_settings.json"
    s = M.ModelSettings(**kw)
    M.save_settings(s, p)
    return p


def test_settings_roundtrip_private_strict(tmp_path):
    p = _settings(tmp_path, preferred="LM-Studio", aliases={
        "LM-Studio": M.ModelAlias("LM-Studio", "custom_local", ("fake-local-model",), 900.0)})
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    s = M.load_settings(p)
    assert s.preferred == "LM-Studio" and s.aliases["LM-Studio"].timeout_sec == 900.0
    for bad in [{"schema": "x"}, {"schema": M.SETTINGS_SCHEMA, "extra": 1},
                {"schema": M.SETTINGS_SCHEMA, "aliases": {"a": {"api_key": "s"}}},
                {"schema": M.SETTINGS_SCHEMA, "aliases": {"a": {"timeout_sec": 99999}}},
                {"schema": M.SETTINGS_SCHEMA, "aliases": {"A": {}, "a": {}}},
                {"schema": M.SETTINGS_SCHEMA, "preferred": "-x"}]:
        with pytest.raises(M.ModelSelectionError):
            M.parse_settings(bad)


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
def test_settings_symlink_refused(tmp_path):
    (tmp_path / "real.json").write_text("{}")
    os.symlink(tmp_path / "real.json", tmp_path / "link.json")
    with pytest.raises(M.ModelSelectionError):
        M.load_settings(tmp_path / "link.json")


def test_resolution_order_and_no_silent_fallback():
    s = M.ModelSettings("LM-Studio", {"LM-Studio": M.ModelAlias("LM-Studio", "custom_local", ("fake-local-model",))})
    assert M.resolve_model("grok-4.6", s).source == "per_job"
    c = M.resolve_model(None, s)
    assert (c.model_id, c.source, c.kind, c.timeout_ceiling_sec) == ("LM-Studio", "owner_preference", "custom_local", 1800.0)
    assert c.expect_usage_keys == ("LM-Studio", "fake-local-model") and c.availability == "unverified"
    d = M.resolve_model(None, s, use_preference=False)
    assert d.model_id is None and d.source == "implicit_default" and d.describe()["implicit_default_note"]
    u = M.resolve_model("some-other", s)
    assert u.kind == "unregistered" and u.timeout_ceiling_sec == 600.0
    with pytest.raises(M.ModelSelectionError):
        M.resolve_model("--yolo", s)


def _plan(model, t=180.0):
    return P.build_wsl_grok_ask_plan(task_id="t1", distro="Ubuntu", user="okern", grok_path="/usr/bin/grok",
                                     cwd=CWD, prompt="hi", timeout_sec=t, model=model)


def test_plan_passes_model_verbatim_and_keeps_isolation():
    base = _plan(None)
    c = M.resolve_model("LM-Studio", M.ModelSettings())
    p = _plan(c)
    i = p.argv.index("--model")
    assert p.argv[i + 1] == "LM-Studio" and "--model" not in base.argv and "-m" not in p.argv
    assert p.argv.index("--single") < i  # model flag is in the fixed tail, after the prompt value
    assert [a for a in p.argv if a not in ("--model", "LM-Studio")] == list(base.argv)
    for flag in ("--always-approve", "--yolo", "bypassPermissions"):
        assert flag not in p.argv
    assert p.expected_models == ("LM-Studio",) and base.expected_models is None


def test_timeout_ceiling_only_raised_for_local_alias():
    loc = M.resolve_model("LM-Studio", M.ModelSettings(None, {"LM-Studio": M.ModelAlias("LM-Studio")}))
    pl = _plan(loc, 1800.0)
    assert pl.timeout_sec == 1830.0 and pl.argv[pl.argv.index('/usr/bin/timeout') + 3] == '1800'
    for c in (None, M.resolve_model("grok-4.6", M.ModelSettings())):
        with pytest.raises(P.PlanError):
            _plan(c, 900.0)
    with pytest.raises(P.PlanError):
        _plan(loc, 1801.0)


def _summ(mu, expected):
    out = {"text": "x", "stopReason": "end_turn"}
    if mu is not ...:
        out["modelUsage"] = mu
    return parse_grok_json_result(json.dumps(out), expected_models=expected)


@pytest.mark.parametrize("mu,expected,ident,code", [
    ({"fake-local-model": {}}, ("LM-Studio", "fake-local-model"), "confirmed", None),
    ({"grok-4.6": {}}, ("LM-Studio", "fake-local-model"), "mismatch", "model_identity_mismatch"),
    ({"fake-local-model": {}, "grok-4.6": {}}, ("LM-Studio", "fake-local-model"), "mismatch", "model_identity_mismatch"),
    ({}, ("LM-Studio",), "unreported", "model_identity_unverified"),
    (..., ("LM-Studio",), "unreported", "model_identity_unverified"),
    ({"bad key\n": {}}, ("LM-Studio",), "mismatch", "model_identity_mismatch"),
    ({"grok-4.6": {}}, None, "not_requested", None),
])
def test_identity_from_model_usage(mu, expected, ident, code):
    s = _summ(mu, expected)
    assert s.model_identity == ident and classify(s, require_expected=False) == code
    d = s.to_dict()
    assert validate_summary(d) and "grok-4.6" not in json.dumps(d) and "fake-local" not in json.dumps(d)


def test_v1_rows_still_validate_v2_strict():
    d = _summ(..., None).to_dict()
    v1 = {k: v for k, v in d.items() if k != "model_identity"}
    v1["schema"] = SUMMARY_SCHEMA_V1
    assert validate_summary(v1)
    assert not validate_summary({**d, "model_identity": "maybe"})
    assert not validate_summary({**v1, "model_identity": "confirmed"})


def _exec(tmp_path, monkeypatch, mode, extra):
    _mock_wsl(monkeypatch)
    cap = []
    _real_dummy(monkeypatch, mode, cap)
    rc = cli.main(BASE + ["--prompt", "hi", "--execute", "--allow-model-call",
                          "--state-db", str(tmp_path / "s.db"), *extra])
    return rc, cap


def test_e2e_local_alias_confirmed_mismatch_unreported(tmp_path, capsys, monkeypatch):
    sp = _settings(tmp_path, preferred="LM-Studio", aliases={
        "LM-Studio": M.ModelAlias("LM-Studio", "custom_local", ("fake-local-model",), 900.0)})
    rc, cap = _exec(tmp_path / "a", monkeypatch, "mu_local", ["--model-settings", str(sp)])
    d = json.loads(capsys.readouterr().out)
    assert rc == 0 and d["status"] == "succeeded" and d["model"]["source"] == "owner_preference"
    assert cap[0][cap[0].index("--model") + 1] == "LM-Studio"
    assert d["result_summary"]["model_identity"] == "confirmed"
    raw = (tmp_path / "a" / "s.db").read_bytes()
    assert b"LM-Studio" not in raw and b"fake-local-model" not in raw  # names never persisted
    for mode, code in (("mu_grok", "model_identity_mismatch"), ("mu_empty", "model_identity_unverified")):
        rc, _ = _exec(tmp_path / mode, monkeypatch, mode, ["--model-settings", str(sp)])
        d = json.loads(capsys.readouterr().out)
        assert rc != 0 and d["status"] == "needs_review" and d["error_code"] == code


def test_e2e_implicit_default_is_honest(tmp_path, capsys, monkeypatch):
    sp = _settings(tmp_path, preferred="LM-Studio")
    rc, cap = _exec(tmp_path / "b", monkeypatch, "mu_grok", ["--model-settings", str(sp), "--no-model-preference"])
    d = json.loads(capsys.readouterr().out)
    assert rc == 0 and "--model" not in cap[0] and d["model"]["source"] == "implicit_default"
    assert d["result_summary"]["model_identity"] == "not_requested"


def test_dry_run_preview_no_writes_no_grok_models(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_wsl_list_reader", lambda: pytest.fail("no host call"))
    monkeypatch.setattr(cli, "_wsl_cmd_runner", lambda a: pytest.fail("no host call"))
    sp = tmp_path / "none.json"
    rc = cli.main(BASE + ["--prompt", "hi", "--model", "LM-Studio", "--model-settings", str(sp), "--format", "text"])
    out = capsys.readouterr().out
    assert rc == 0 and "LM-Studio" in out and "not checked" in out and "free or offline" in out
    assert not sp.exists() and list(tmp_path.iterdir()) == []
    capsys.readouterr()
    rc = cli.main(BASE + ["--prompt", "hi", "--model=--yolo"])
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"]["code"] == "model_id_invalid"


def test_model_settings_cli(tmp_path, capsys):
    sp = str(tmp_path / "ms.json")
    assert cli.main(["model-settings", "--settings", sp, "add-alias", "LM-Studio",
                     "--expect-usage-key", "fake-local-model", "--timeout-sec", "1200"]) == 0
    assert cli.main(["model-settings", "--settings", sp, "add-alias", "lm-studio"]) == 2
    assert cli.main(["model-settings", "--settings", sp, "set-preferred", "LM-Studio"]) == 0
    capsys.readouterr()
    assert cli.main(["model-settings", "--settings", sp, "show"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["preferred"] == "LM-Studio" and "NOT verified" in d["note"]
    assert d["aliases"]["LM-Studio"] == {"kind": "custom_local", "expect_usage_keys": ["fake-local-model"], "timeout_sec": 1200.0}
    assert cli.main(["model-settings", "--settings", sp, "add-alias", "x", "--timeout-sec", "5000"]) == 2
    assert cli.main(["model-settings", "--settings", sp, "set-preferred", "--clear"]) == 0
    assert cli.main(["model-settings", "--settings", sp, "remove-alias", "LM-Studio"]) == 0
    assert json.loads(open(sp).read())["aliases"] == {}

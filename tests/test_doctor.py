import json
import socket
from pathlib import Path

import pytest

from grok_bot_build_bridge import cli
from grok_bot_build_bridge.doctor import run_doctor, render_text
from grok_bot_build_bridge.safe_exec import RunResult


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*a, **k):
        raise AssertionError("doctor must not open network connections")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def _ids(rep):
    return {c["id"]: c for c in rep["checks"]}


def test_fixture_default_is_labelled_demo(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    assert cli.main(["doctor"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["mode"] == "fixture" and "not this machine" in rep["scope_note"]
    assert rep["capabilities"]["model_auth_verified"] is False
    assert _ids(rep)["model_auth"]["status"] == "unknown"
    assert not (tmp_path / "d").exists() and not (tmp_path / "c").exists()
    assert cli.main(["doctor", "--format", "text"]) == 0
    assert "(demo data only)" in capsys.readouterr().out


def _runner(table):
    calls = []

    def run(argv, **kw):
        calls.append(tuple(argv))
        for key, res in table.items():
            if key in " ".join(argv):
                return res(argv) if callable(res) else res
        return RunResult(tuple(argv), None, "", "", error="executable_not_found")
    run.calls = calls
    return run


def test_live_absent_everything():
    rep = run_doctor(mode="live", runner=_runner({}))
    ids = _ids(rep)
    assert ids["grok_native"]["status"] == "absent" and ids["wsl_distros"]["status"] == "absent"
    assert ids["wsl_grok"]["status"] == "skipped" and not rep["capabilities"]["ask_plannable_via_wsl"]


def test_live_stopped_distro_never_started():
    wsl_list = "  NAME      STATE           VERSION\n* Ubuntu    Stopped         2\n"
    r = _runner({"-l -v": RunResult(("wsl.exe",), 0, wsl_list, "")})
    rep = run_doctor(mode="live", runner=r)
    ids = _ids(rep)
    assert ids["wsl_distros"]["status"] == "not_ready" and ids["wsl_grok"]["status"] == "skipped"
    # only the listing ran; nothing executed inside the stopped distro
    assert all("-d" not in c for c in r.calls if c and "wsl" in c[0].lower())


def test_live_wsl_list_timeout_is_unknown():
    r = _runner({"-l -v": RunResult(("wsl.exe",), None, "", "", timed_out=True, error="timeout")})
    rep = run_doctor(mode="live", runner=r)
    ids = _ids(rep)
    assert ids["wsl_distros"]["status"] == "unknown" and ids["probe_timeouts"]["status"] == "unknown"


def test_state_db_absent_not_created_and_present_read_only(tmp_path):
    missing = tmp_path / "x" / "s.db"
    rep = run_doctor(state_db=str(missing))
    assert _ids(rep)["state_db"]["status"] == "absent" and not missing.parent.exists()
    from grok_bot_build_bridge.state import JobStore
    db = tmp_path / "s.db"
    JobStore(db).enqueue_job(task_id="a", backend="wsl", cwd="/tmp")
    import hashlib
    h = hashlib.sha256(db.read_bytes()).hexdigest()
    rep = run_doctor(state_db=str(db))
    assert _ids(rep)["state_db"]["status"] == "ok" and _ids(rep)["state_db"]["jobs"] == 1
    assert hashlib.sha256(db.read_bytes()).hexdigest() == h
    garbage = tmp_path / "g.db"
    garbage.write_bytes(b"nope" * 50)
    assert _ids(run_doctor(state_db=str(garbage)))["state_db"]["status"] == "not_ready"


def test_text_never_claims_model_ready():
    t = render_text(run_doctor())
    assert "readiness for a real model call is unknown" in t
    assert "model_call" in run_doctor()["never_done"]

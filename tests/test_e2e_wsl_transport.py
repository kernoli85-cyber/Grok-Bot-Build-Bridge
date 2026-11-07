"""E2E: discovery -> explicit WSL plan -> durable job -> runner -> outbox dry-run.

Truthful separation: plan_backend vs execution_transport. No real wsl.exe, grok,
model, or network in CI (fake executor / monkeypatched list reader).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from grok_bot_build_bridge import cli
from grok_bot_build_bridge.discovery import classify_discovery, nonroot_ready
from grok_bot_build_bridge.models import JobRequest
from grok_bot_build_bridge.routing import plan_job
from grok_bot_build_bridge.runner import build_approved_plan, execute_one_shot
from grok_bot_build_bridge.runner.plan import (
    PlanError,
    build_wsl_probe_plan,
    require_wsl_distro_running,
)
from grok_bot_build_bridge.safe_exec import RunResult
from grok_bot_build_bridge.state import JobStore
from grok_bot_build_bridge.transport.pump import pump_outbox

FIX = Path(__file__).parent / "fixtures"
RUNNING = "  NAME      STATE           VERSION\n* Ubuntu    Running         2\n"
STOPPED = "  NAME      STATE           VERSION\n* Ubuntu    Stopped         2\n"


def _report():
    cand = nonroot_ready(
        distro="Ubuntu", user="gbbbdemo", uid=1000, distro_state="Running",
        grok_path="/home/gbbbdemo/.local/bin/grok",
        version_text="grok 1.0.70 (f50904eaee6c) [stable]",
    )
    return classify_discovery(
        native_path=None, native_version_text=None,
        wsl_list_text=RUNNING, wsl_candidates=(cand,),
    )


def test_full_chain_discovery_to_outbox_dry_run(tmp_path):
    report = _report()
    assert report.kind == "wsl_only"
    jp = plan_job(report, JobRequest(task_id="e2e-1", prompt="noop", cwd="/home/gbbbdemo", backend="wsl"))
    assert jp.backend == "wsl"
    cand = report.wsl[0]
    plan = build_wsl_probe_plan(task_id="e2e-1", distro=cand.distro, user=cand.user, cwd=jp.cwd)
    require_wsl_distro_running(cand.distro, RUNNING)
    seen = {}

    def fake_exec(argv, **kw):
        seen["argv"] = tuple(argv)
        return RunResult(tuple(argv), 0, "bridge-wsl-probe task=e2e-1\n", "")

    store = JobStore(tmp_path / "s.db")
    out = execute_one_shot(store, plan, owner="w", dry_run=False, executor=fake_exec)
    assert seen["argv"][:8] == ("wsl.exe", "-d", "Ubuntu", "-u", "gbbbdemo", "--cd", "/home/gbbbdemo", "--exec")
    assert seen["argv"][8] == "/usr/bin/printf"
    assert out.status == "succeeded"
    assert (out.plan_backend, out.execution_transport, out.execution_kind) == ("wsl", "wsl_exe", "wsl_printf_probe")
    types = [e.event_type for e in store.list_outbox()]
    assert "job.succeeded" in types and "job.running" in types
    res = pump_outbox(store, None, dry_run=True)
    assert res and all(r["dry_run"] for r in res)


def test_probe_refuses_root_unsafe_and_relative():
    with pytest.raises(PlanError) as e:
        build_wsl_probe_plan(task_id="t", distro="Ubuntu", user="root", cwd="/tmp")
    assert e.value.code == "wsl_root_refused"
    for kw in ({"distro": "My Distro"}, {"user": "a;b"}, {"cwd": "relative"}, {"cwd": "/a/../b"}):
        args = {"task_id": "t", "distro": "Ubuntu", "user": "gbbbdemo", "cwd": "/tmp", **kw}
        with pytest.raises(PlanError):
            build_wsl_probe_plan(**args)


def test_never_start_stopped_or_unlisted():
    with pytest.raises(PlanError) as e:
        require_wsl_distro_running("Ubuntu", STOPPED)
    assert e.value.code == "wsl_distro_not_running"
    with pytest.raises(PlanError) as e:
        require_wsl_distro_running("Debian", RUNNING)
    assert e.value.code == "wsl_distro_not_listed"


def test_dummy_with_wsl_plan_is_labelled_host_not_wsl(tmp_path):
    plan = build_approved_plan(task_id="d1", backend="wsl", cwd="/tmp",
                               executable=str(FIX / "bridge-dummy.py"), prompt="x")
    assert plan.execution_transport in {"host_python", "host_direct_posix"}
    assert plan.execution_transport != "wsl_exe"
    out = execute_one_shot(JobStore(tmp_path / "d.db"), plan, owner="w", dry_run=False)
    assert out.status == "succeeded"
    assert cli._transport_note(out.plan_backend, out.execution_transport) == \
        "plan_backend_wsl_but_spawned_on_host_NOT_wsl_execution"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="windows-only guard")
def test_windows_wsl_grok_direct_spawn_refused(tmp_path):
    g = tmp_path / "grok.exe"
    g.write_bytes(b"")
    with pytest.raises(PlanError) as e:
        build_approved_plan(task_id="g", backend="wsl", cwd="/home/x", executable=str(g))
    assert e.value.code == "wsl_transport_required"


def test_cli_probe_dry_run_and_stopped_refusal(tmp_path, capsys, monkeypatch):
    base = ["runner-run", "--state-db", str(tmp_path / "c.db"), "--task-id", "c1",
            "--cwd", "/home/gbbbdemo", "--wsl-probe", "--wsl-distro", "Ubuntu", "--wsl-user", "gbbbdemo"]
    called = []
    monkeypatch.setattr(cli, "_wsl_list_reader", lambda: (called.append(1), (STOPPED, None))[1])
    assert cli.main(base) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["dry_run"] is True and d["execution_transport"] == "wsl_exe"
    assert d["grok_executed"] is False and not called
    assert cli.main(base + ["--execute"]) == 2
    d = json.loads(capsys.readouterr().out)
    assert d["error"]["code"] == "wsl_distro_not_running"
    assert JobStore(tmp_path / "c.db").get_job("c1") is None

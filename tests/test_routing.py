from __future__ import annotations

from grok_bot_build_bridge.discovery import classify_discovery, nonroot_ready
from grok_bot_build_bridge.models import JobRequest, RoutingError
from grok_bot_build_bridge.routing import plan_job, select_backend, validate_cwd


def _wsl_report():
    cand = nonroot_ready(
        "Ubuntu", "okern", 1000, "Running", "/home/okern/.local/bin/grok", "grok 1.0.70"
    )
    return classify_discovery(
        native_path=None, native_version_text=None, wsl_candidates=(cand,)
    )


def _both_report():
    cand = nonroot_ready(
        "Ubuntu", "okern", 1000, "Running", "/home/okern/.local/bin/grok", "grok 1.0.70"
    )
    return classify_discovery(
        native_path=r"C:\grok\grok.exe",
        native_version_text="grok 1.0.70",
        wsl_candidates=(cand,),
    )


def test_no_silent_fallback_when_requested_offline():
    report = _wsl_report()
    err = select_backend(report, "native")
    assert isinstance(err, RoutingError)
    assert err.code == "backend_not_ready"


def test_default_used_when_omitted():
    report = _wsl_report()
    assert select_backend(report, None) == "wsl"


def test_both_without_default_errors():
    report = _both_report()
    err = select_backend(report, None)
    assert isinstance(err, RoutingError)
    assert err.code == "default_required"


def test_cwd_reject_windows_path_on_wsl():
    err = validate_cwd("wsl", r"C:\Users\okern\project")
    assert err is not None
    assert err.code == "cwd_backend_mismatch"


def test_cwd_reject_mnt_on_native():
    err = validate_cwd("native", "/mnt/c/Users/okern/project")
    assert err is not None
    assert err.code == "cwd_backend_mismatch"


def test_plan_job_wsl_ok():
    report = _wsl_report()
    plan = plan_job(
        report,
        JobRequest(
            task_id="t1",
            prompt="explain API",
            cwd="/home/okern/proj",
            max_turns=3,
            deny_rules=("Bash(rm *)",),
        ),
    )
    assert not isinstance(plan, RoutingError)
    assert plan.backend == "wsl"
    assert plan.argv[0:3] == ("grok", "--single", "explain API")
    assert "--cwd" in plan.argv
    assert plan.env["GROK_BOT_BUILD_BRIDGE_TASK_ID"] == "t1"


def test_plan_rejects_mismatch_cwd():
    report = _wsl_report()
    err = plan_job(
        report,
        JobRequest(task_id="t1", prompt="x", cwd=r"C:\nope"),
    )
    assert isinstance(err, RoutingError)
    assert err.code == "cwd_backend_mismatch"

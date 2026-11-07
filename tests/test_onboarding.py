from __future__ import annotations

import json
from pathlib import Path

import pytest

from grok_bot_build_bridge.cli import main
from grok_bot_build_bridge.discovery import classify_discovery, nonroot_ready
from grok_bot_build_bridge.models import BackendState, NativeBackend
from grok_bot_build_bridge.onboarding import (
    PairingStatus,
    OnboardingPlan,
    onboarding_plan_to_dict,
    plan_onboarding,
    probe_pairing_metadata,
)
from grok_bot_build_bridge.models import DiscoveryReport

FIX = Path(__file__).parent / "fixtures"


def _wsl_ready():
    return nonroot_ready(
        "Ubuntu", "gbbbdemo", 1000, "Running", "/home/gbbbdemo/.local/bin/grok", "grok 1.0.70"
    )


def _native_ready():
    return classify_discovery(
        native_path=r"C:\grok\grok.exe",
        native_version_text="grok 1.0.70",
        wsl_candidates=(),
    )


def test_unset_desired_needs_choice_even_wsl_only():
    report = classify_discovery(
        native_path=None, native_version_text=None, wsl_candidates=(_wsl_ready(),)
    )
    plan = plan_onboarding(
        report,
        desired="unset",
        pairing=probe_pairing_metadata(Path("/no/such/webhook-onboard.json")),
    )
    assert plan.needs_choice is True
    assert plan.outcome == "needs_choice"
    assert plan.suggested_default_backend is None
    d = onboarding_plan_to_dict(plan)
    assert d["pairing"]["auth_ready_claim"] is False


def test_matrix_none():
    report = classify_discovery(native_path=None, native_version_text=None, wsl_candidates=())
    plan = plan_onboarding(
        report,
        desired="wsl",
        pairing=probe_pairing_metadata(Path("/no/such/x.json")),
    )
    assert report.kind == "none"
    assert plan.outcome in {"blocked", "partial"}
    assert "desired_wsl_but_wsl_not_ready" in plan.blockers


def test_matrix_native_only_with_desired():
    report = _native_ready()
    plan = plan_onboarding(
        report,
        desired="native",
        pairing=probe_pairing_metadata(Path("/no/such/x.json")),
    )
    assert plan.needs_choice is False
    assert plan.suggested_default_backend == "native"
    assert plan.outcome == "partial"  # pairing unconfigured


def test_matrix_both_desired_both_ready_still_needs_default_choice():
    cand = _wsl_ready()
    report = classify_discovery(
        native_path=r"C:\grok\grok.exe",
        native_version_text="grok 1.0.70",
        wsl_candidates=(cand,),
    )
    assert report.kind == "both"
    plan = plan_onboarding(
        report,
        desired="both",
        pairing=probe_pairing_metadata(Path("/no/such/x.json")),
    )
    assert plan.needs_choice is True
    assert plan.suggested_default_backend is None


def test_stopped_distro_never_autostart_hint():
    stopped = nonroot_ready(
        "Ubuntu", "gbbbdemo", 1000, "Stopped", "/home/gbbbdemo/.local/bin/grok", "grok 1.0.70"
    )
    report = classify_discovery(
        native_path=None, native_version_text=None, wsl_candidates=(stopped,)
    )
    plan = plan_onboarding(report, desired="wsl", pairing=probe_pairing_metadata(Path("/no/x")))
    assert any(c.state == BackendState.STOPPED for c in report.wsl)
    assert any("will NOT start" in i.detail or "NOT start" in i.detail for i in plan.checklist)
    assert "ask_user_to_start_stopped_distro_manually" in plan.next_steps


def test_not_ready_distinct_from_absent():
    report = classify_discovery(
        native_path=r"C:\grok\grok.exe",
        native_version_text="not-a-version",
        wsl_candidates=(),
    )
    assert report.native.state == BackendState.NOT_READY
    plan = plan_onboarding(report, desired="native", pairing=probe_pairing_metadata(Path("/no/x")))
    native_items = [i for i in plan.checklist if i.id == "native_backend"]
    assert native_items[0].status == "blocked"
    assert "distinct from absent" in native_items[0].detail


def test_pairing_config_present_not_auth_ready(tmp_path: Path):
    cfg = tmp_path / "webhook.json"
    cfg.write_text('{"url":"https://example.com","bearer_token":"SHOULD_NOT_BE_READ"}\n', encoding="utf-8")
    probe = probe_pairing_metadata(cfg)
    assert probe.status == PairingStatus.CONFIG_PRESENT
    # Ensure planner did not need to read — re-read file ourselves only in test to prove content untouched
    plan = plan_onboarding(_native_ready(), desired="native", pairing=probe)
    blob = json.dumps(onboarding_plan_to_dict(plan))
    assert "SHOULD_NOT_BE_READ" not in blob
    assert plan.pairing == PairingStatus.CONFIG_PRESENT
    assert onboarding_plan_to_dict(plan)["pairing"]["auth_ready_claim"] is False


def test_idempotent_plan_dict():
    report = classify_discovery(
        native_path=None, native_version_text=None, wsl_candidates=(_wsl_ready(),)
    )
    pairing = probe_pairing_metadata(Path("/no/idempotent.json"))
    a = onboarding_plan_to_dict(plan_onboarding(report, desired="wsl", pairing=pairing))
    b = onboarding_plan_to_dict(plan_onboarding(report, desired="wsl", pairing=pairing))
    assert a == b


def test_partial_discovery_probe_notes():
    report = classify_discovery(native_path=None, native_version_text=None, wsl_candidates=())
    plan = plan_onboarding(
        report,
        desired="unset",
        pairing=probe_pairing_metadata(Path("/no/x")),
        discovery_probe_notes=("probe_timeout:wsl_list:wsl.exe", "probe_absent_tool:native_lookup:pwsh"),
    )
    assert "probe_timeout:wsl_list:wsl.exe" in plan.warnings
    assert plan.needs_choice is True


def test_version_mismatch_native_path_without_version():
    report = classify_discovery(
        native_path=r"C:\grok\grok.exe",
        native_version_text=None,
        wsl_candidates=(),
    )
    assert report.native.state == BackendState.NOT_READY
    assert "native_path_without_version" in report.warnings
    plan = plan_onboarding(report, desired="native", pairing=probe_pairing_metadata(Path("/no")))
    assert "desired_native_but_native_not_ready" in plan.blockers


def test_cli_onboard_dry_run_fixture(capsys):
    rc = main(["onboard", "--desired", "unset"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["dry_run"] is True
    assert data["needs_choice"] is True
    assert data["schema_version"] == 1
    assert data["pairing"]["auth_ready_claim"] is False


def test_cli_onboard_apply_rejected(capsys):
    rc = main(["onboard", "--apply"])
    assert rc == 2
    data = json.loads(capsys.readouterr().out)
    assert data["error"]["code"] == "apply_not_supported"


def test_future_gates_documented():
    plan = plan_onboarding(
        _native_ready(),
        desired="native",
        pairing=probe_pairing_metadata(Path("/no")),
    )
    assert any("bearer_secret_home" in g for g in plan.future_gates)
    assert any("bot_plugin_packaging" in g for g in plan.future_gates)

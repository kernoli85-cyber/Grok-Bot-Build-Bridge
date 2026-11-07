from __future__ import annotations

import json
from pathlib import Path

import pytest

from grok_bot_build_bridge.bot_onboard_adapter import (
    PlanContractError,
    build_conversation_view,
    conversation_view_to_dict,
    parse_onboarding_plan_payload,
    validate_operator_choices,
)
from grok_bot_build_bridge.cli import main
from grok_bot_build_bridge.discovery import classify_discovery, nonroot_ready
from grok_bot_build_bridge.onboarding import (
    onboarding_plan_to_dict,
    plan_onboarding,
    probe_pairing_metadata,
)
from grok_bot_build_bridge.preferences import (
    OperatorPreferences,
    PreferenceError,
    load_preferences,
    save_preferences,
)


def _plan(desired="unset", kind="wsl"):
    if kind == "wsl":
        cand = nonroot_ready(
            "Ubuntu", "okern", 1000, "Running", "/home/okern/.local/bin/grok", "grok 1.0.70"
        )
        report = classify_discovery(
            native_path=None, native_version_text=None, wsl_candidates=(cand,)
        )
    elif kind == "native":
        report = classify_discovery(
            native_path=r"C:\grok\grok.exe",
            native_version_text="grok 1.0.70",
            wsl_candidates=(),
        )
    elif kind == "both":
        cand = nonroot_ready(
            "Ubuntu", "okern", 1000, "Running", "/home/okern/.local/bin/grok", "grok 1.0.70"
        )
        report = classify_discovery(
            native_path=r"C:\grok\grok.exe",
            native_version_text="grok 1.0.70",
            wsl_candidates=(cand,),
        )
    else:
        report = classify_discovery(
            native_path=None, native_version_text=None, wsl_candidates=()
        )
    return onboarding_plan_to_dict(
        plan_onboarding(
            report,
            desired=desired,
            pairing=probe_pairing_metadata(Path("/no/webhook-bot-adapter.json")),
        )
    )


def test_parse_happy_schema_v1():
    data = parse_onboarding_plan_payload(_plan())
    assert data["schema_version"] == 1
    assert data["pairing"]["auth_ready_claim"] is False


def test_reject_unknown_schema():
    p = _plan()
    p["schema_version"] = 99
    with pytest.raises(PlanContractError) as ei:
        parse_onboarding_plan_payload(p)
    assert ei.value.code == "unsupported_schema"


def test_reject_malformed_json():
    with pytest.raises(PlanContractError) as ei:
        parse_onboarding_plan_payload(b"{not-json")
    assert ei.value.code == "plan_malformed"


def test_reject_oversized():
    huge = json.dumps({"schema_version": 1, "pad": "x" * (300_000)}).encode()
    with pytest.raises(PlanContractError) as ei:
        parse_onboarding_plan_payload(huge)
    assert ei.value.code == "plan_too_large"


def test_redact_sensitive_keys_in_payload():
    p = _plan()
    p["checklist"].append(
        {
            "id": "x",
            "status": "info",
            "title": "t",
            "detail": "d",
            "prompt": "SECRET_PROMPT_VALUE",
            "action_hint": None,
        }
    )
    # prompt is extra key inside item — redact_mapping redacts by key name at mapping level
    # but checklist cleaner only keeps allowlisted fields — so prompt dropped
    data = parse_onboarding_plan_payload(p)
    blob = json.dumps(data)
    assert "SECRET_PROMPT_VALUE" not in blob


def test_force_auth_ready_false_even_if_set():
    p = _plan()
    p["pairing"]["auth_ready_claim"] = True
    data = parse_onboarding_plan_payload(p)
    assert data["pairing"]["auth_ready_claim"] is False


def test_checklist_never_treat_as_shell():
    view = build_conversation_view(_plan())
    d = conversation_view_to_dict(view)
    assert all(i["treat_as_shell"] is False for i in d["checklist"])
    assert d["distinction"]["desired"]


def test_choices_distinct_desired_vs_discovery():
    view = build_conversation_view(_plan(desired="unset", kind="wsl"))
    d = conversation_view_to_dict(view)
    assert d["discovery_kind"] == "wsl_only"
    assert d["plan_desired"] == "unset"
    kinds = {c["kind"] for c in d["choices"]}
    assert "desired" in kinds and "default_backend" in kinds


def test_validate_choices_both_requires_default():
    with pytest.raises(PlanContractError) as ei:
        validate_operator_choices(
            desired="both", default_backend=None, discovery_kind="both"
        )
    assert ei.value.code == "choice_default_required"


def test_roundtrip_onboard_choices_replan_matrix(tmp_path: Path):
    for kind, desired, default in [
        ("none", "wsl", "wsl"),
        ("native", "native", "native"),
        ("wsl", "wsl", "wsl"),
        ("both", "both", "native"),
    ]:
        plan = _plan(desired="unset", kind=kind)
        view = build_conversation_view(plan)
        assert view.discovery_kind in {
            "none",
            "native_only",
            "wsl_only",
            "both",
        }
        choices = validate_operator_choices(
            desired=desired,
            default_backend=default,
            discovery_kind=view.discovery_kind,
        )
        prefs_path = tmp_path / f"prefs-{kind}.json"
        save_preferences(
            OperatorPreferences(
                schema_version=1,
                desired=choices.desired,  # type: ignore[arg-type]
                default_backend=choices.default_backend,  # type: ignore[arg-type]
                discovery_kind_at_save=choices.discovery_kind_at_choice,
                notes=choices.notes,
            ),
            prefs_path,
            overwrite=False,
        )
        # replan with saved desired
        plan2 = _plan(desired=desired, kind=kind)
        view2 = build_conversation_view(
            plan2, saved_desired=desired, saved_default=default
        )
        assert view2.plan_desired == desired
        if kind == "none" and desired == "wsl":
            assert view2.stale_hint or choices.notes


def test_stale_discovery_hint():
    plan = _plan(desired="native", kind="wsl")
    view = build_conversation_view(plan, saved_desired="native", saved_default="native")
    assert view.stale_hint is not None


def test_prefs_no_clobber(tmp_path: Path):
    path = tmp_path / "preferences.json"
    prefs = OperatorPreferences(
        schema_version=1,
        desired="wsl",
        default_backend="wsl",
        discovery_kind_at_save="wsl_only",
        notes=(),
    )
    save_preferences(prefs, path, overwrite=False)
    with pytest.raises(PreferenceError) as ei:
        save_preferences(prefs, path, overwrite=False)
    assert ei.value.code == "exists_no_clobber"
    save_preferences(prefs, path, overwrite=True)
    loaded = load_preferences(path)
    assert loaded is not None and loaded.desired == "wsl"


def test_prefs_reject_forbidden_keys(tmp_path: Path):
    path = tmp_path / "preferences.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "desired": "wsl",
                "default_backend": "wsl",
                "bearer_token": "NO",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PreferenceError) as ei:
        load_preferences(path)
    assert ei.value.code == "unknown_keys"


def test_cli_onboard_view_and_prefs(tmp_path: Path, capsys):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(desired="unset", kind="wsl")), encoding="utf-8")
    assert main(["onboard-view", "--plan-json", str(plan_path)]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["pairing"]["auth_ready_claim"] is False
    prefs = tmp_path / "preferences.json"
    assert (
        main(
            [
                "prefs-save",
                "--desired",
                "wsl",
                "--default-backend",
                "wsl",
                "--discovery-kind",
                "wsl_only",
                "--path",
                str(prefs),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["prefs-show", "--path", str(prefs)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["preferences"]["desired"] == "wsl"


def test_draft_skill_present_and_generic():
    skill = Path(__file__).resolve().parents[1] / "drafts/skills/grok-bot-build-bridge-einrichtung/SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert "Grok Bot-Build Bridge Einrichtung" in text
    assert "Balu" not in text
    assert "kernoli" not in text.lower()
    assert "marketplace" in text.lower()
    assert "Not** marketplace" in text or "not marketplace" in text.lower()

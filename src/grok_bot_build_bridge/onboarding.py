"""Read-only onboarding planner — checklist for Bot-guided setup.

Pure functions over DiscoveryReport + pairing *metadata*. Never installs,
never starts stopped WSL distros, never reads webhook secrets, never POSTs.
CLI `onboard` is a diagnostic companion; Bot skill is the intended UX.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Sequence

from .models import BackendId, BackendState, DiscoveryKind, DiscoveryReport
from .transport.webhook import default_webhook_config_path

ONBOARDING_SCHEMA_VERSION = 1

DesiredPreference = Literal["unset", "native", "wsl", "both"]
ChecklistStatus = Literal["done", "todo", "blocked", "needs_choice", "info"]
PlanOutcome = Literal["ready", "needs_choice", "blocked", "partial"]


class PairingStatus(str, Enum):
    """Config-file metadata only — never implies auth-ready."""

    UNCONFIGURED = "unconfigured"  # path absent
    CONFIG_PRESENT = "config_present"  # file exists; NOT auth-ready
    UNKNOWN = "unknown"  # existence check failed


@dataclass(frozen=True)
class ChecklistItem:
    id: str
    status: ChecklistStatus
    title: str
    detail: str
    action_hint: str | None = None


@dataclass(frozen=True)
class PairingProbe:
    status: PairingStatus
    config_path: str
    note: str


@dataclass(frozen=True)
class OnboardingPlan:
    schema_version: int
    dry_run: bool
    outcome: PlanOutcome
    discovery_kind: DiscoveryKind
    desired: DesiredPreference
    needs_choice: bool
    suggested_default_backend: BackendId | None
    per_job_backend_note: str
    pairing: PairingStatus
    pairing_config_path: str
    pairing_note: str
    checklist: tuple[ChecklistItem, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    next_steps: tuple[str, ...]
    future_gates: tuple[str, ...]
    discovery_summary: dict[str, Any]


_PAIRING_NOTE = (
    "Local webhook.json convention under user config root; "
    "file presence ≠ auth-ready; bearer never read by onboard planner; "
    "Bot vault storage / plugin packaging remain future gates"
)

_FUTURE_GATES = (
    "bearer_secret_home: retain local-only webhook.json convention for now; Bot-managed vault is a future gate",
    "bot_plugin_packaging: Bot extension guided setup is product UX; this CLI is diagnostic companion only",
    "runner_cancel_integration: jobs-cancel remains state-only until separate safety review",
)


def probe_pairing_metadata(path: Path | None = None) -> PairingProbe:
    """Existence/stat only — does not open or parse webhook secrets."""
    cfg = Path(path) if path is not None else default_webhook_config_path()
    try:
        exists = cfg.is_file()
    except OSError as exc:
        return PairingProbe(
            status=PairingStatus.UNKNOWN,
            config_path=str(cfg),
            note=f"existence_check_failed:{type(exc).__name__}; {_PAIRING_NOTE}",
        )
    if not exists:
        return PairingProbe(
            status=PairingStatus.UNCONFIGURED,
            config_path=str(cfg),
            note=_PAIRING_NOTE,
        )
    # Intentionally do not read contents / validate token.
    return PairingProbe(
        status=PairingStatus.CONFIG_PRESENT,
        config_path=str(cfg),
        note=_PAIRING_NOTE,
    )


def _native_item(report: DiscoveryReport) -> ChecklistItem:
    n = report.native
    if n.state == BackendState.READY:
        return ChecklistItem(
            id="native_backend",
            status="done",
            title="Native Grok Build ready",
            detail=f"version={n.version} path_set=yes",
        )
    if n.state == BackendState.NOT_FOUND:
        return ChecklistItem(
            id="native_backend",
            status="todo",
            title="Native Grok Build absent",
            detail="distinct_from_error: not_found",
            action_hint="Offer install link/checklist only — do not auto-install",
        )
    if n.state == BackendState.NOT_READY:
        return ChecklistItem(
            id="native_backend",
            status="blocked",
            title="Native Grok Build not ready",
            detail="present_but_not_ready (version unparsed / probe incomplete) — distinct from absent",
            action_hint="Ask operator to verify grok --version; do not invent ready",
        )
    return ChecklistItem(
        id="native_backend",
        status="blocked",
        title="Native backend unexpected state",
        detail=n.state.value,
    )


def _wsl_items(report: DiscoveryReport) -> list[ChecklistItem]:
    items: list[ChecklistItem] = []
    if not report.wsl:
        items.append(
            ChecklistItem(
                id="wsl_backend",
                status="todo",
                title="WSL / Grok in WSL absent",
                detail="no_wsl_candidates; distinct_from_probe_error if tools missing",
                action_hint="Offer WSL2 + non-root grok checklist — never auto-install or start distros",
            )
        )
        return items
    for i, c in enumerate(report.wsl):
        cid = f"wsl_candidate:{c.distro}:{i}"
        if c.state == BackendState.READY:
            items.append(
                ChecklistItem(
                    id=cid,
                    status="done",
                    title=f"WSL {c.distro} ready (non-root)",
                    detail=f"user={c.user} uid={c.uid} version={c.version}",
                )
            )
        elif c.state == BackendState.STOPPED:
            items.append(
                ChecklistItem(
                    id=cid,
                    status="todo",
                    title=f"WSL {c.distro} stopped",
                    detail="stopped_distinct_from_absent; bridge will NOT start distro",
                    action_hint="Ask user to start distro manually; never wsl --distribute start from bridge",
                )
            )
        elif c.state == BackendState.NOT_FOUND:
            items.append(
                ChecklistItem(
                    id=cid,
                    status="todo",
                    title=f"WSL {c.distro}: grok absent",
                    detail="grok_not_on_path; distinct_from_stopped_and_error",
                    action_hint="Offer grok install checklist inside distro — no auto-install",
                )
            )
        else:  # NOT_READY
            items.append(
                ChecklistItem(
                    id=cid,
                    status="blocked",
                    title=f"WSL {c.distro} not ready",
                    detail=f"notes={list(c.notes)}; distinct_from_absent",
                    action_hint="Remediate root user / version / probe issues; never treat as ready",
                )
            )
    return items


def _preference_item(
    desired: DesiredPreference, report: DiscoveryReport
) -> tuple[ChecklistItem, bool, BackendId | None, list[str]]:
    """Returns item, needs_choice, suggested_default, blockers."""
    ready = report.ready_backends()
    blockers: list[str] = []
    suggested: BackendId | None = None

    if desired == "unset":
        return (
            ChecklistItem(
                id="desired_preference",
                status="needs_choice",
                title="Desired backend preference unset",
                detail="missing desired → needs_choice; planner will not silently pick",
                action_hint="Bot/UI must ask: native | wsl | both",
            ),
            True,
            None,
            blockers,
        )

    if desired == "native":
        if "native" not in ready:
            blockers.append("desired_native_but_native_not_ready")
            return (
                ChecklistItem(
                    id="desired_preference",
                    status="blocked",
                    title="Desired native not ready",
                    detail=f"ready={sorted(ready)}",
                    action_hint="Complete native checklist or change desired preference",
                ),
                False,
                None,
                blockers,
            )
        suggested = "native"
        return (
            ChecklistItem(
                id="desired_preference",
                status="done",
                title="Desired native matches ready backend",
                detail="default=native; per-job may still select native only when ready",
            ),
            False,
            suggested,
            blockers,
        )

    if desired == "wsl":
        if "wsl" not in ready:
            blockers.append("desired_wsl_but_wsl_not_ready")
            return (
                ChecklistItem(
                    id="desired_preference",
                    status="blocked",
                    title="Desired WSL not ready",
                    detail=f"ready={sorted(ready)}",
                    action_hint="Start stopped distro manually / install grok non-root, or change desired",
                ),
                False,
                None,
                blockers,
            )
        suggested = "wsl"
        return (
            ChecklistItem(
                id="desired_preference",
                status="done",
                title="Desired WSL matches ready backend",
                detail="default=wsl; per-job may still select wsl only when ready",
            ),
            False,
            suggested,
            blockers,
        )

    # desired == both
    if ready != frozenset({"native", "wsl"}):
        blockers.append("desired_both_but_not_both_ready")
        return (
            ChecklistItem(
                id="desired_preference",
                status="blocked" if not ready else "todo",
                title="Desired both — matrix incomplete",
                detail=f"ready={sorted(ready)} kind={report.kind}",
                action_hint="Finish missing backend checklist; still no silent fallback",
            ),
            False,
            None,
            blockers,
        )
    # both ready — still need explicit default among the two for jobs without per-job backend
    return (
        ChecklistItem(
            id="desired_preference",
            status="needs_choice",
            title="Both backends desired and ready — default still required",
            detail="kind=both: set explicit default native|wsl; per-job backend overrides",
            action_hint="Ask default backend; never parallel-start; never silent fallback",
        ),
        True,
        None,
        blockers,
    )


def plan_onboarding(
    report: DiscoveryReport,
    *,
    desired: DesiredPreference = "unset",
    pairing: PairingProbe | None = None,
    discovery_probe_notes: Sequence[str] = (),
    dry_run: bool = True,
) -> OnboardingPlan:
    """Build an idempotent onboarding plan. No I/O except optional pairing probe."""
    if desired not in ("unset", "native", "wsl", "both"):
        raise ValueError("desired_invalid")

    pair = pairing if pairing is not None else probe_pairing_metadata()
    checklist: list[ChecklistItem] = []
    warnings = list(report.warnings)
    warnings.extend(discovery_probe_notes)

    checklist.append(
        ChecklistItem(
            id="discovery",
            status="done" if report.kind != "none" or report.wsl or report.native.state != BackendState.NOT_FOUND else "todo",
            title=f"Discovery kind={report.kind}",
            detail=f"ready={sorted(report.ready_backends())} default_hint={report.default_backend}",
        )
    )
    checklist.append(_native_item(report))
    checklist.extend(_wsl_items(report))

    pref_item, needs_choice, suggested, blockers = _preference_item(desired, report)
    checklist.append(pref_item)

    # Pairing
    if pair.status == PairingStatus.UNCONFIGURED:
        checklist.append(
            ChecklistItem(
                id="pairing",
                status="todo",
                title="Notify pairing unconfigured",
                detail="webhook config file absent (metadata only)",
                action_hint="Operator places local webhook.json outside repo — onboard will not write secrets",
            )
        )
    elif pair.status == PairingStatus.CONFIG_PRESENT:
        checklist.append(
            ChecklistItem(
                id="pairing",
                status="info",
                title="Notify config file present",
                detail="config_present ≠ auth-ready; secrets not read; no smoke POST",
                action_hint="Validate separately via outbox-pump dry-run; --send is explicit later",
            )
        )
    else:
        checklist.append(
            ChecklistItem(
                id="pairing",
                status="blocked",
                title="Notify pairing unknown",
                detail="could not stat config path",
                action_hint="Check path permissions; do not invent configured",
            )
        )
        blockers.append("pairing_probe_unknown")

    checklist.append(
        ChecklistItem(
            id="no_auto_install",
            status="info",
            title="No prerequisite installs from planner",
            detail="Links/checklist only; stopped distros never started",
        )
    )

    # Outcome
    if blockers and not needs_choice:
        outcome: PlanOutcome = "blocked"
    elif needs_choice:
        outcome = "needs_choice"
    elif report.kind == "none":
        outcome = "partial"
    elif desired == "both" and report.kind != "both":
        outcome = "partial"
    elif pair.status == PairingStatus.UNCONFIGURED:
        outcome = "partial"
    else:
        outcome = "ready"

    next_steps: list[str] = []
    if needs_choice:
        next_steps.append("collect_desired_preference_native_wsl_or_both")
    if report.kind == "none":
        next_steps.append("offer_install_links_only_no_auto_install")
    if any(c.state == BackendState.STOPPED for c in report.wsl):
        next_steps.append("ask_user_to_start_stopped_distro_manually")
    if pair.status == PairingStatus.UNCONFIGURED:
        next_steps.append("document_local_webhook_json_path_no_secret_collection_here")
    if outcome == "ready" and suggested:
        next_steps.append(f"use_default_backend={suggested}_with_per_job_override")
    if not next_steps:
        next_steps.append("review_checklist_with_operator")

    summary = {
        "kind": report.kind,
        "native_state": report.native.state.value,
        "native_version": report.native.version,
        "wsl": [
            {
                "distro": c.distro,
                "state": c.state.value,
                "user": c.user,
                "uid": c.uid,
                "version": c.version,
                "notes": list(c.notes),
            }
            for c in report.wsl
        ],
        "ready": sorted(report.ready_backends()),
        "discovery_default_backend": report.default_backend,
    }

    return OnboardingPlan(
        schema_version=ONBOARDING_SCHEMA_VERSION,
        dry_run=bool(dry_run),
        outcome=outcome,
        discovery_kind=report.kind,
        desired=desired,
        needs_choice=needs_choice,
        suggested_default_backend=suggested,
        per_job_backend_note=(
            "Per-job backend must be explicit when overriding default; "
            "no silent fallback; never parallel native+wsl start"
        ),
        pairing=pair.status,
        pairing_config_path=pair.config_path,
        pairing_note=pair.note,
        checklist=tuple(checklist),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        next_steps=tuple(next_steps),
        future_gates=_FUTURE_GATES,
        discovery_summary=summary,
    )


def onboarding_plan_to_dict(plan: OnboardingPlan) -> dict[str, Any]:
    """Stable structured JSON for Bot skill / CLI — no secrets."""
    return {
        "schema_version": plan.schema_version,
        "dry_run": plan.dry_run,
        "outcome": plan.outcome,
        "discovery_kind": plan.discovery_kind,
        "desired": plan.desired,
        "needs_choice": plan.needs_choice,
        "suggested_default_backend": plan.suggested_default_backend,
        "per_job_backend_note": plan.per_job_backend_note,
        "pairing": {
            "status": plan.pairing.value,
            "config_path": plan.pairing_config_path,
            "note": plan.pairing_note,
            "auth_ready_claim": False,
        },
        "checklist": [
            {
                "id": c.id,
                "status": c.status,
                "title": c.title,
                "detail": c.detail,
                "action_hint": c.action_hint,
            }
            for c in plan.checklist
        ],
        "blockers": list(plan.blockers),
        "warnings": list(plan.warnings),
        "next_steps": list(plan.next_steps),
        "future_gates": list(plan.future_gates),
        "discovery_summary": plan.discovery_summary,
        "product_note": (
            "Bot extension guided setup is the product UX; "
            "CLI onboard is a read-only diagnostic companion"
        ),
    }

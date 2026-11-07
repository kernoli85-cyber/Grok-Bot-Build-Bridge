"""Bot-facing onboarding adapter: strict JSON in → conversation descriptors out.

Never executes plan strings as shell/commands. Never claims auth-ready.
Consumes onboarding schema_version=1 only; rejects unknown/malformed/oversized input.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from .onboarding import (
    ONBOARDING_SCHEMA_VERSION,
    DesiredPreference,
    PairingStatus,
    PlanOutcome,
    ChecklistStatus,
)

MAX_PLAN_BYTES = 256 * 1024
MAX_STRING_CHARS = 2_000
MAX_CHECKLIST_ITEMS = 64
MAX_LIST_ITEMS = 64

_SENSITIVE_KEY = re.compile(
    r"(prompt|token|secret|bearer|password|authorization|api[_-]?key|"
    r"private[_-]?key|webhook_key|stdin|transcript)",
    re.IGNORECASE,
)

_ALLOWED_OUTCOMES = frozenset({"ready", "needs_choice", "blocked", "partial"})
_ALLOWED_DESIRED = frozenset({"unset", "native", "wsl", "both"})
_ALLOWED_KINDS = frozenset({"none", "native_only", "wsl_only", "both"})
_ALLOWED_CHECKLIST_STATUS = frozenset(
    {"done", "todo", "blocked", "needs_choice", "info"}
)
_ALLOWED_PAIRING = frozenset({"unconfigured", "config_present", "unknown"})


class PlanContractError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ChecklistViewItem:
    id: str
    status: ChecklistStatus
    title: str
    detail: str
    action_hint: str | None
    # Explicit: checklist text is display data, never a shell/instruction channel
    treat_as_shell: Literal[False] = False


@dataclass(frozen=True)
class ChoiceDescriptor:
    """Widget-ready choice — selection must be explicit in chat/forms."""

    id: str
    kind: Literal["desired", "default_backend"]
    value: str
    label: str
    enabled: bool
    reason: str | None
    # desired = operator preference; discovered_kind is separate field on view
    applies_to: Literal["operator_preference", "job_default"]


@dataclass(frozen=True)
class ConversationView:
    schema_version: int
    outcome: PlanOutcome
    discovery_kind: str
    plan_desired: DesiredPreference
    needs_choice: bool
    suggested_default_backend: str | None
    pairing_status: str
    pairing_auth_ready_claim: Literal[False]
    checklist: tuple[ChecklistViewItem, ...]
    choices: tuple[ChoiceDescriptor, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    next_steps: tuple[str, ...]
    product_note: str
    stale_hint: str | None


@dataclass(frozen=True)
class ValidatedChoices:
    """Explicit operator selections distinct from discovery_kind."""

    desired: DesiredPreference  # native|wsl|both (not unset for persistence)
    default_backend: str | None  # native|wsl; required when desired==both
    discovery_kind_at_choice: str
    notes: tuple[str, ...]


def _clip_str(value: Any, *, limit: int = MAX_STRING_CHARS) -> str:
    if value is None:
        return ""
    text = str(value)
    # Strip NULs / control chars except newline/tab
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    if len(text) > limit:
        return text[:limit] + "…<truncated>"
    return text


def _redact_mapping(obj: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "<max_depth>"
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in list(obj.items())[:MAX_LIST_ITEMS]:
            key = _clip_str(k, limit=128)
            if _SENSITIVE_KEY.search(key):
                out[key] = "<redacted>"
            else:
                out[key] = _redact_mapping(v, depth=depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact_mapping(x, depth=depth + 1) for x in list(obj)[:MAX_LIST_ITEMS]]
    if isinstance(obj, (bytes, bytearray)):
        return "<binary_redacted>"
    if isinstance(obj, str):
        return _clip_str(obj)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _clip_str(obj)


def parse_onboarding_plan_payload(raw: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    """Strict parse of onboarding plan JSON. Returns sanitized dict or raises."""
    if isinstance(raw, Mapping):
        data = dict(raw)
        # approximate size
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_PLAN_BYTES:
            raise PlanContractError("plan_too_large", "plan exceeds size limit")
    else:
        if isinstance(raw, str):
            blob = raw.encode("utf-8")
        else:
            blob = bytes(raw)
        if len(blob) > MAX_PLAN_BYTES:
            raise PlanContractError("plan_too_large", "plan exceeds size limit")
        try:
            data = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanContractError("plan_malformed", f"json_decode_failed:{type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise PlanContractError("plan_malformed", "root must be object")

    data = _redact_mapping(data)
    if not isinstance(data, dict):
        raise PlanContractError("plan_malformed", "redact produced non-object")

    ver = data.get("schema_version")
    if ver != ONBOARDING_SCHEMA_VERSION:
        raise PlanContractError(
            "unsupported_schema",
            f"schema_version={ver!r}; only {ONBOARDING_SCHEMA_VERSION} supported",
        )

    outcome = data.get("outcome")
    if outcome not in _ALLOWED_OUTCOMES:
        raise PlanContractError("invalid_outcome", f"outcome={outcome!r}")

    kind = data.get("discovery_kind")
    if kind not in _ALLOWED_KINDS:
        raise PlanContractError("invalid_discovery_kind", f"discovery_kind={kind!r}")

    desired = data.get("desired")
    if desired not in _ALLOWED_DESIRED:
        raise PlanContractError("invalid_desired", f"desired={desired!r}")

    if not isinstance(data.get("needs_choice"), bool):
        raise PlanContractError("invalid_needs_choice", "needs_choice must be bool")

    pairing = data.get("pairing")
    if not isinstance(pairing, dict):
        raise PlanContractError("invalid_pairing", "pairing object required")
    pst = pairing.get("status")
    if pst not in _ALLOWED_PAIRING:
        raise PlanContractError("invalid_pairing_status", f"status={pst!r}")
    # Force auth_ready_claim false even if attacker set true
    pairing["auth_ready_claim"] = False
    pairing["status"] = pst
    pairing["config_path"] = _clip_str(pairing.get("config_path"), limit=512)
    pairing["note"] = _clip_str(pairing.get("note"))
    data["pairing"] = pairing

    checklist = data.get("checklist")
    if not isinstance(checklist, list):
        raise PlanContractError("invalid_checklist", "checklist must be list")
    if len(checklist) > MAX_CHECKLIST_ITEMS:
        raise PlanContractError("checklist_too_long", "too many checklist items")
    clean_items = []
    for item in checklist:
        if not isinstance(item, dict):
            raise PlanContractError("invalid_checklist_item", "item must be object")
        st = item.get("status")
        if st not in _ALLOWED_CHECKLIST_STATUS:
            raise PlanContractError("invalid_checklist_status", f"status={st!r}")
        clean_items.append(
            {
                "id": _clip_str(item.get("id"), limit=128),
                "status": st,
                "title": _clip_str(item.get("title")),
                "detail": _clip_str(item.get("detail")),
                "action_hint": (
                    _clip_str(item.get("action_hint"))
                    if item.get("action_hint") is not None
                    else None
                ),
            }
        )
    data["checklist"] = clean_items

    for key in ("blockers", "warnings", "next_steps", "future_gates"):
        val = data.get(key, [])
        if not isinstance(val, list):
            raise PlanContractError(f"invalid_{key}", f"{key} must be list")
        data[key] = [_clip_str(x) for x in val[:MAX_LIST_ITEMS]]

    sug = data.get("suggested_default_backend")
    if sug is not None and sug not in ("native", "wsl"):
        raise PlanContractError("invalid_suggested_default", f"suggested={sug!r}")

    # discovery_summary: redact only, keep as object
    summary = data.get("discovery_summary", {})
    if summary is None:
        summary = {}
    if not isinstance(summary, dict):
        raise PlanContractError("invalid_discovery_summary", "must be object")
    data["discovery_summary"] = _redact_mapping(summary)

    data["dry_run"] = bool(data.get("dry_run", True))
    data["per_job_backend_note"] = _clip_str(data.get("per_job_backend_note", ""))
    data["product_note"] = _clip_str(
        data.get(
            "product_note",
            "Bot extension guided setup; CLI onboard is diagnostic companion only; "
            "not marketplace-installable yet",
        )
    )
    return data


def build_conversation_view(
    plan: Mapping[str, Any],
    *,
    saved_desired: DesiredPreference | None = None,
    saved_default: str | None = None,
) -> ConversationView:
    """Render checklist + choice descriptors for future chat widgets."""
    data = parse_onboarding_plan_payload(plan)
    kind = data["discovery_kind"]
    ready = set()
    summary = data.get("discovery_summary") or {}
    if isinstance(summary.get("ready"), list):
        ready = {str(x) for x in summary["ready"]}

    checklist = tuple(
        ChecklistViewItem(
            id=i["id"],
            status=i["status"],  # type: ignore[arg-type]
            title=i["title"],
            detail=i["detail"],
            action_hint=i["action_hint"],
        )
        for i in data["checklist"]
    )

    choices: list[ChoiceDescriptor] = [
        ChoiceDescriptor(
            id="desired_native",
            kind="desired",
            value="native",
            label="Prefer Native Grok Build",
            enabled=True,
            reason="Operator preference (may be ahead of discovery)",
            applies_to="operator_preference",
        ),
        ChoiceDescriptor(
            id="desired_wsl",
            kind="desired",
            value="wsl",
            label="Prefer WSL Grok Build",
            enabled=True,
            reason="Operator preference (may be ahead of discovery)",
            applies_to="operator_preference",
        ),
        ChoiceDescriptor(
            id="desired_both",
            kind="desired",
            value="both",
            label="Want both backends available",
            enabled=True,
            reason="Requires both ready before default can be set",
            applies_to="operator_preference",
        ),
        ChoiceDescriptor(
            id="default_native",
            kind="default_backend",
            value="native",
            label="Default jobs to native",
            enabled="native" in ready or kind in ("native_only", "both"),
            reason=None if ("native" in ready or kind in ("native_only", "both")) else "native not in current discovery ready set",
            applies_to="job_default",
        ),
        ChoiceDescriptor(
            id="default_wsl",
            kind="default_backend",
            value="wsl",
            label="Default jobs to WSL",
            enabled="wsl" in ready or kind in ("wsl_only", "both"),
            reason=None if ("wsl" in ready or kind in ("wsl_only", "both")) else "wsl not in current discovery ready set",
            applies_to="job_default",
        ),
    ]

    stale = None
    if saved_desired and saved_desired != "unset":
        if saved_desired == "native" and kind in ("none", "wsl_only"):
            stale = "saved_desired_native_but_discovery_lacks_native"
        elif saved_desired == "wsl" and kind in ("none", "native_only"):
            stale = "saved_desired_wsl_but_discovery_lacks_wsl"
        elif saved_desired == "both" and kind != "both":
            stale = "saved_desired_both_but_discovery_not_both"
        if saved_default and saved_default not in ready and kind != "both":
            # default pointing at non-ready backend
            if stale is None:
                stale = f"saved_default_{saved_default}_not_in_ready"

    return ConversationView(
        schema_version=ONBOARDING_SCHEMA_VERSION,
        outcome=data["outcome"],  # type: ignore[arg-type]
        discovery_kind=kind,
        plan_desired=data["desired"],  # type: ignore[arg-type]
        needs_choice=bool(data["needs_choice"]),
        suggested_default_backend=data.get("suggested_default_backend"),
        pairing_status=data["pairing"]["status"],
        pairing_auth_ready_claim=False,
        checklist=checklist,
        choices=tuple(choices),
        blockers=tuple(data["blockers"]),
        warnings=tuple(data["warnings"]),
        next_steps=tuple(data["next_steps"]),
        product_note=data["product_note"],
        stale_hint=stale,
    )


def conversation_view_to_dict(view: ConversationView) -> dict[str, Any]:
    return {
        "schema_version": view.schema_version,
        "outcome": view.outcome,
        "discovery_kind": view.discovery_kind,
        "plan_desired": view.plan_desired,
        "needs_choice": view.needs_choice,
        "suggested_default_backend": view.suggested_default_backend,
        "pairing": {
            "status": view.pairing_status,
            "auth_ready_claim": False,
        },
        "checklist": [
            {
                "id": c.id,
                "status": c.status,
                "title": c.title,
                "detail": c.detail,
                "action_hint": c.action_hint,
                "treat_as_shell": False,
            }
            for c in view.checklist
        ],
        "choices": [
            {
                "id": c.id,
                "kind": c.kind,
                "value": c.value,
                "label": c.label,
                "enabled": c.enabled,
                "reason": c.reason,
                "applies_to": c.applies_to,
            }
            for c in view.choices
        ],
        "blockers": list(view.blockers),
        "warnings": list(view.warnings),
        "next_steps": list(view.next_steps),
        "product_note": view.product_note,
        "stale_hint": view.stale_hint,
        "distinction": {
            "desired": "operator preference (may be aspirational)",
            "discovery_kind": "currently discovered matrix from last plan",
            "default_backend": "job default when per-job backend omitted",
        },
    }


def validate_operator_choices(
    *,
    desired: str,
    default_backend: str | None,
    discovery_kind: str,
) -> ValidatedChoices:
    """Require explicit desired; default required when desired=both."""
    if desired not in ("native", "wsl", "both"):
        raise PlanContractError(
            "choice_desired_invalid",
            "desired must be explicit native|wsl|both (unset not allowed for save)",
        )
    if discovery_kind not in _ALLOWED_KINDS:
        raise PlanContractError("choice_discovery_invalid", "discovery_kind invalid")
    if desired == "both":
        if default_backend not in ("native", "wsl"):
            raise PlanContractError(
                "choice_default_required",
                "desired=both requires explicit default_backend native|wsl",
            )
    else:
        if default_backend is None:
            default_backend = desired  # type: ignore[assignment]
        elif default_backend not in ("native", "wsl"):
            raise PlanContractError("choice_default_invalid", "default_backend invalid")
        elif default_backend != desired:
            raise PlanContractError(
                "choice_default_mismatch",
                "when desired is a single backend, default_backend must match",
            )
    notes: list[str] = []
    if desired == "native" and discovery_kind in ("none", "wsl_only"):
        notes.append("stale_or_aspirational:desired_native_vs_discovery")
    if desired == "wsl" and discovery_kind in ("none", "native_only"):
        notes.append("stale_or_aspirational:desired_wsl_vs_discovery")
    if desired == "both" and discovery_kind != "both":
        notes.append("stale_or_aspirational:desired_both_vs_discovery")
    return ValidatedChoices(
        desired=desired,  # type: ignore[arg-type]
        default_backend=default_backend,
        discovery_kind_at_choice=discovery_kind,
        notes=tuple(notes),
    )

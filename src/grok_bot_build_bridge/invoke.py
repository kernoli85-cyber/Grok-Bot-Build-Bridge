"""One stdin JSON request, one JSON response, for text ask or selected context.

Fixed argv is ``grok-bot-build-bridge invoke`` (no flags). Intended to be started
by ``wsl.exe --exec`` with no shell. The envelope cannot set the environment,
choose an arbitrary executable argv, or relax tool, sandbox, turn, or consent gates.

POSIX only. A Windows source tree is not read.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from typing import Any

SCHEMA = "gbbb.invoke/v1"
MAX_ENVELOPE_BYTES = 16 * 1024
_OPS = frozenset({"ask", "ask-context", "propose"})
_COMMON = frozenset({
    "schema", "op", "executable", "model", "timeout_sec", "max_turns",
    "execute", "allow_model_call", "confirm_send", "show_answer",
})
_ASK = _COMMON | {"prompt"}
_CONTEXT = _COMMON | {"source", "files", "question"}
_PROPOSE = (_COMMON - {"show_answer"}) | {
    "source", "files", "instruction", "show_proposal", "show_rejected_reply", "replacement_format",
}
_FORMATS = ("json-v1", "literal-v1", "native-schema-v1")
_MODEL_KEYS = frozenset({"id", "kind", "expect_usage_keys"})
_EXE_BAD = set(";&|$`<>(){}[]!*?~#\\\"'\n\r\t ")


class InvokeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _out(obj: dict, code: int) -> int:
    json.dump(obj, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return code


def _err(code: str, message: str) -> int:
    return _out({"error": {"code": code, "message": message}}, 2)


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def parse_envelope(raw: bytes) -> dict:
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise InvokeError("envelope_too_large", f"envelope exceeds {MAX_ENVELOPE_BYTES} bytes")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise InvokeError("envelope_not_utf8", "envelope must be UTF-8") from None
    if text.startswith("\ufeff"):
        text = text[1:]
    decoder = json.JSONDecoder(object_pairs_hook=_no_dupes)
    try:
        obj, end = decoder.raw_decode(text.lstrip())
    except (ValueError, RecursionError):
        raise InvokeError("envelope_invalid", "stdin must be one JSON object") from None
    if text.lstrip()[end:].strip():
        raise InvokeError("envelope_invalid", "stdin must be exactly one JSON object")
    if not isinstance(obj, dict):
        raise InvokeError("envelope_invalid", "stdin must be one JSON object")
    if obj.get("schema") != SCHEMA:
        raise InvokeError("schema_invalid", f"schema must be {SCHEMA}")
    op = obj.get("op")
    if op not in _OPS:
        raise InvokeError("schema_invalid", "op must be ask, ask-context, or propose")
    allowed = {"ask": _ASK, "ask-context": _CONTEXT, "propose": _PROPOSE}[op]
    if set(obj) - allowed:
        raise InvokeError("schema_invalid", "envelope has unknown fields")
    if not {"executable", "model"} <= set(obj):
        raise InvokeError("schema_invalid", "executable and model are required")
    if op == "ask" and "prompt" not in obj:
        raise InvokeError("schema_invalid", "ask requires prompt")
    if op == "ask-context" and not {"source", "files", "question"} <= set(obj):
        raise InvokeError("schema_invalid", "ask-context requires source, files, and question")
    if op == "propose" and not {"source", "files", "instruction"} <= set(obj):
        raise InvokeError("schema_invalid", "propose requires source, files, and instruction")
    return obj


def _bool(obj: dict, name: str) -> bool:
    if name not in obj:
        return False
    value = obj[name]
    if not isinstance(value, bool):
        raise InvokeError("field_invalid", f"{name} must be a boolean")
    return value


def _executable(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/") or ".." in value.split("/"):
        raise InvokeError("field_invalid", "executable must be an absolute path")
    if any(ch in _EXE_BAD for ch in value):
        raise InvokeError("field_invalid", "executable must not contain whitespace or shell metacharacters")
    return value


def _max_turns(obj: dict) -> int:
    number = obj.get("max_turns", 1)
    if isinstance(number, bool) or number != 1:
        raise InvokeError("field_invalid", "max_turns must be 1")
    return 1


def _model_choice(obj: dict, timeout_sec: float | None):
    from .model_select import LOCAL_TIMEOUT_CEILING_SEC, ModelChoice, ModelSelectionError, validate_model_id

    model = obj["model"]
    if not isinstance(model, dict) or set(model) != _MODEL_KEYS:
        raise InvokeError("schema_invalid", "model must contain id, kind, and expect_usage_keys")
    if model["kind"] != "custom_local":
        raise InvokeError("model_identity_required", "invoke accepts only kind custom_local")
    keys = model["expect_usage_keys"]
    if not isinstance(keys, list) or not 1 <= len(keys) <= 8:
        raise InvokeError("model_identity_required", "expect_usage_keys must be 1..8 provider model ids")
    try:
        mid = validate_model_id(model["id"])
        usage = tuple(validate_model_id(k) for k in keys)
    except ModelSelectionError as exc:
        raise InvokeError(exc.code, exc.message) from None
    expect = tuple(dict.fromkeys((mid, *usage)))
    return ModelChoice(mid, "per_job", "custom_local", expect, LOCAL_TIMEOUT_CEILING_SEC, timeout_sec)


def _timeout(obj: dict) -> float | None:
    if "timeout_sec" not in obj:
        return None
    value = obj["timeout_sec"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvokeError("field_invalid", "timeout_sec must be a number")
    return float(value)


def _checked(obj: dict) -> dict:
    """Schema checks that do not touch the filesystem."""
    execute = _bool(obj, "execute")
    allow = _bool(obj, "allow_model_call")
    show = _bool(obj, "show_answer") if "show_answer" in obj or obj.get("op") != "propose" else False
    show_proposal = _bool(obj, "show_proposal") if obj.get("op") == "propose" else False
    show_rejected = _bool(obj, "show_rejected_reply") if obj.get("op") == "propose" else False
    replacement_format = "json-v1"
    if obj.get("op") == "propose" and "replacement_format" in obj:
        replacement_format = obj["replacement_format"]
        if replacement_format not in _FORMATS:
            raise InvokeError("field_invalid", "replacement_format must be json-v1, literal-v1, or native-schema-v1")
    if show_rejected and not show_proposal:
        raise InvokeError("field_invalid", "show_rejected_reply requires show_proposal")
    if "confirm_send" in obj and not isinstance(obj["confirm_send"], str):
        raise InvokeError("field_invalid", "confirm_send must be a string")
    if not execute and (allow or show or "confirm_send" in obj):
        raise InvokeError("field_invalid", "allow_model_call, show_answer, and confirm_send require execute")
    if not execute and obj.get("op") != "propose" and (show_proposal or show_rejected):
        raise InvokeError("field_invalid", "show_proposal requires execute")
    if execute and not allow:
        raise InvokeError("model_call_gated", "real call requires allow_model_call")
    if execute and "confirm_send" not in obj:
        raise InvokeError("send_not_confirmed", "real call requires confirm_send from the dry-run")
    _max_turns(obj)
    executable = _executable(obj["executable"])
    timeout = _timeout(obj)
    choice = _model_choice(obj, timeout)
    return {"execute": execute, "show": show, "show_proposal": show_proposal,
            "show_rejected": show_rejected, "replacement_format": replacement_format,
            "executable": executable, "timeout": timeout, "choice": choice}


def _shape(argv, prompt: str, placeholder: str) -> list[str]:
    return [("<PROMPT>" if item == prompt else "<CWD>" if item == placeholder else item) for item in argv]


def _binding(op: str, executable: str, choice, timeout: float, selection: list[str], argv, placeholder, prompt: str,
             *, show_rejected_reply: bool = False, replacement_format: str = "json-v1",
             native: tuple[str, str] | None = None) -> dict:
    binding = {
        "op": op, "backend": "posix-local", "executable": executable,
        "executable_realpath": os.path.realpath(executable),
        "model_id": choice.model_id, "model_kind": choice.kind, "model_source": choice.source,
        "expect_usage_keys": list(choice.expect_usage_keys), "max_turns": 1,
        "timeout_sec": timeout, "selection": selection,
        "argv_shape_sha256": hashlib.sha256(
            json.dumps(_shape(argv, prompt, placeholder)).encode()).hexdigest(),
    }
    if op == "propose":
        binding["show_rejected_reply"] = bool(show_rejected_reply)
        binding["replacement_format"] = replacement_format
        if native is not None:
            binding["native_system_sha256"] = hashlib.sha256(native[0].encode()).hexdigest()
            binding["native_schema_sha256"] = hashlib.sha256(native[1].encode()).hexdigest()
    return binding


def _token(key: bytes, prompt: str, binding: dict) -> str:
    from .context_pack import PackedContext, consent_fingerprint

    packed = PackedContext(prompt, "", 0, 0, len(prompt.encode()), 0)
    return consent_fingerprint(key, packed, binding)


def _prepare(obj: dict, checked: dict) -> dict:
    from .context_cli import LOCAL_PLACEHOLDER_CWD, _stage_and_pack
    from .context_pack import ContextError, load_or_create_consent_key
    from .runner import posix_local as PL
    from .runner.plan import PlanError, validate_ask_prompt
    from .snapshot_posix import SnapshotError
    from .state.paths import user_config_root

    choice = checked["choice"]
    timeout = float(checked["timeout"] if checked["timeout"] is not None else 180.0)
    executable = checked["executable"]
    key_path = str(user_config_root() / "consent.key")
    try:
        key, key_created = load_or_create_consent_key(key_path)
    except ContextError as exc:
        raise InvokeError(exc.code, exc.message) from None
    except OSError:
        raise InvokeError("io_error", "unexpected OSError (details withheld: may contain paths)") from None
    selection: list[str] = []
    summary = None
    skipped = None
    bases: list[tuple[str, str]] = []
    native: tuple[str, str] | None = None
    if obj["op"] == "ask":
        try:
            prompt = validate_ask_prompt(obj["prompt"])
        except PlanError as exc:
            raise InvokeError(exc.code, exc.message) from None
    elif obj["op"] == "propose":
        from .proposal import stage_proposal
        source, files, instruction = obj["source"], obj["files"], obj["instruction"]
        if not isinstance(source, str) or not isinstance(instruction, str) or not isinstance(files, list):
            raise InvokeError("field_invalid", "source, files, and instruction have the wrong type")
        if not all(isinstance(item, str) for item in files):
            raise InvokeError("field_invalid", "files must be strings")
        try:
            packed, texts, skipped = stage_proposal(
                source, list(files), instruction, key, checked["replacement_format"])
        except ContextError as exc:
            raise InvokeError(exc.code, exc.message) from None
        except SnapshotError as exc:
            raise InvokeError(exc.code, exc.message) from None
        except OSError:
            raise InvokeError("io_error", "unexpected OSError (details withheld: may contain paths)") from None
        prompt = packed.prompt
        selection = list(files)
        bases = texts
        summary = packed.summary()
        if checked["replacement_format"] == "native-schema-v1":
            from .proposal import NATIVE_SYSTEM_PROMPT, native_replacement_schema
            native = (NATIVE_SYSTEM_PROMPT, native_replacement_schema(selection))
    else:
        source, files, question = obj["source"], obj["files"], obj["question"]
        if not isinstance(source, str) or not isinstance(question, str) or not isinstance(files, list):
            raise InvokeError("field_invalid", "source, files, and question have the wrong type")
        if not all(isinstance(item, str) for item in files):
            raise InvokeError("field_invalid", "files must be strings")
        try:
            packed, rep = _stage_and_pack(source, list(files), question, key)
        except ContextError as exc:
            raise InvokeError(exc.code, exc.message) from None
        except SnapshotError as exc:
            raise InvokeError(exc.code, exc.message) from None
        except OSError:
            raise InvokeError("io_error", "unexpected OSError (details withheld: may contain paths)") from None
        prompt = packed.prompt
        selection = list(files)
        summary = packed.summary()
        skipped = rep.skipped
    try:
        plan = PL.build_local_plan(
            grok_path=executable, cwd=LOCAL_PLACEHOLDER_CWD, prompt=prompt,
            model=choice, max_turns=1, timeout_sec=timeout, native_structured=native)
    except PL.RunnerError as exc:
        raise InvokeError(exc.code, exc.message) from None
    binding = _binding(obj["op"], executable, choice, timeout, selection, plan.argv, LOCAL_PLACEHOLDER_CWD, prompt,
                       show_rejected_reply=bool(checked.get("show_rejected")),
                       replacement_format=str(checked.get("replacement_format") or "json-v1"),
                       native=native)
    return {
        "prompt": prompt, "selection": selection, "choice": choice, "timeout": timeout,
        "executable": executable, "token": _token(key, prompt, binding), "key_created": key_created,
        "summary": summary, "skipped": skipped, "plan": plan, "bases": bases, "key": key,
        "source": obj.get("source"), "files": list(obj.get("files") or []),
        "instruction": obj.get("instruction"),
        "replacement_format": checked.get("replacement_format") or "json-v1",
        "native": native,
    }


def run_invoke(raw: bytes) -> int:
    try:
        obj = parse_envelope(raw)
        checked = _checked(obj)
    except InvokeError as exc:
        return _err(exc.code, exc.message)
    if os.name != "posix" or sys.platform == "win32":
        return _err("posix_only",
                    "invoke runs inside POSIX (for example WSL); Windows does not read sources or start a listener")
    try:
        info = _prepare(obj, checked)
    except InvokeError as exc:
        return _err(exc.code, exc.message)
    if not checked["execute"]:
        body: dict[str, Any] = {
            "schema": SCHEMA, "mode": "dry_run", "op": obj["op"], "model_call": False, "network": False,
            "stored": False, "db": False, "outbox": False, "mcp": False,
            "writes": {"consent_key_created": info["key_created"], "db": False, "outbox": False},
            "tools": "all denied (text-only)", "max_turns": 1, "timeout_sec": info["timeout"],
            "backend": "posix-local", "consent_token": info["token"],
            "model": {"id": info["choice"].model_id, "kind": info["choice"].kind, "availability": "unverified"},
        }
        if obj["op"] == "ask":
            body["prompt_bytes"] = len(info["prompt"].encode())
            body["prompt_stored"] = False
        else:
            body["context"] = info["summary"]
            body["snapshot_skipped"] = info["skipped"]
            body["writes"]["staging_dir_removed"] = True
        if obj["op"] == "propose":
            body["applied"] = False
            body["source_writes"] = False
            body["proposal_only"] = True
            body["replacement_format"] = info["replacement_format"]
        return _out(body, 0)

    from .context_cli import LOCAL_PLACEHOLDER_CWD
    from .runner import posix_local as PL

    if not hmac.compare_digest(obj["confirm_send"], info["token"]):
        return _err("consent_mismatch", "token does not match this envelope; dry-run again")
    try:
        PL.validate_executable(info["executable"])
        cwd = PL.make_private_cwd()
    except PL.RunnerError as exc:
        return _err(exc.code, exc.message)
    try:
        real = PL.build_local_plan(
            grok_path=info["executable"], cwd=cwd, prompt=info["prompt"],
            model=info["choice"], max_turns=1, timeout_sec=info["timeout"],
            native_structured=info.get("native"))
    except PL.RunnerError as exc:
        PL._cleanup_cwd(cwd)
        return _err(exc.code, exc.message)
    if _shape(real.argv, info["prompt"], cwd) != _shape(info["plan"].argv, info["prompt"], LOCAL_PLACEHOLDER_CWD):
        PL._cleanup_cwd(cwd)
        return _err("consent_mismatch", "launch argv differs from the confirmed plan")
    res = PL.run_local(
        real, show_answer=checked["show"], capture_text=(obj["op"] == "propose"),
        structured_channel=(obj["op"] == "propose" and info.get("replacement_format") == "native-schema-v1"))
    captured = res.pop("captured_text", None)
    captured_meta = res.pop("captured_meta", None) or {}
    structured_state = res.pop("structured_state", None)
    structured_value = res.pop("structured_value", None)
    body = {
        "schema": SCHEMA, "mode": "execute", "op": obj["op"], "model_call": True,
        "stored": False, "db": False, "outbox": False, "mcp": False, "retry": False,
        "result": res,
    }
    if obj["op"] != "propose":
        return _out(body, 0 if res["status"] == "succeeded" else 3)
    body["applied"] = False
    body["source_writes"] = False
    if res["status"] != "succeeded":
        body["status"] = "execution_" + res["status"]
        return _out(body, 3)
    from .context_pack import ContextError
    from .proposal import (
        ProposalError, model_text_diagnostic, parse_proposal, proposal_nonce, review, stage_proposal,
    )
    from .snapshot_posix import SnapshotError
    native_mode = info.get("replacement_format") == "native-schema-v1"
    if native_mode:
        diagnostic = {
            "parsed_channel": "structuredOutput",
            "structured_state": structured_state or "absent",
            "text_channel_ignored": True,
            "stdout_capture_truncated": bool(res.get("stdout_truncated")),
        }
    else:
        diagnostic = model_text_diagnostic(
            captured, extraction_key=str(captured_meta.get("key") or "none"),
            alternate_present=bool(captured_meta.get("alternate_present")),
            alternate_bytes=captured_meta.get("alternate_bytes"),
            fields_equal=bool(captured_meta.get("fields_equal")),
            stdout_truncated=bool(res.get("stdout_truncated")))
    try:
        _packed, now, _skipped = stage_proposal(
            info["source"], info["files"], info["instruction"], info["key"], info["replacement_format"])
        if list(now) != list(info["bases"]):
            raise ProposalError("source_changed", "selected files changed during the call")
        nonce = proposal_nonce(info["key"], list(info["bases"]), str(info["instruction"] or "").strip())
        if native_mode:
            from .proposal import validate_replacement_object
            if structured_state != "value":
                code = {
                    "error": "structured_output_rejected",
                    "ambiguous": "structured_output_ambiguous",
                    "invalid": "structured_output_invalid",
                }.get(structured_state or "", "structured_output_absent")
                raise ProposalError(code, "native structured output was not one object")
            proposed = validate_replacement_object(structured_value, info["selection"])
        else:
            proposed = parse_proposal(captured or "", info["selection"], fmt=info["replacement_format"], nonce=nonce)
        body["proposal"] = review(
            info["selection"], dict(info["bases"]), proposed, info["key"],
            show=checked["show_proposal"], fmt=info["replacement_format"])
    except (ProposalError, ContextError, SnapshotError) as exc:
        body["status"] = "proposal_rejected"
        proposal = {"schema": "gbbb.proposal/v1", "accepted": False, "applied": False,
                    "source_writes": False, "stored": False, "error": exc.code,
                    "diagnostic": diagnostic}
        if checked["show_rejected"] and checked["show_proposal"]:
            from .runner.grok_result import sanitize_display_text
            if native_mode and structured_state == "value" and isinstance(structured_value, dict):
                shown, cut = sanitize_display_text(json.dumps(structured_value, sort_keys=True))
            elif native_mode:
                shown, cut = "", False
            else:
                shown, cut = sanitize_display_text(captured or "")
            if shown:
                proposal["rejected_reply"] = shown
                proposal["rejected_reply_cut"] = cut
        body["proposal"] = proposal
        return _out(body, 3)
    body["status"] = "proposal_accepted"
    return _out(body, 0)


def cmd_invoke(_args) -> int:
    raw = sys.stdin.buffer.read(MAX_ENVELOPE_BYTES + 1)
    return run_invoke(raw)

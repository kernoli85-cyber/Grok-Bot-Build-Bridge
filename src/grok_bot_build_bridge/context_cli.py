"""`ask-context`: explicit files from a local POSIX source -> fenced 8 KiB prompt -> consent token.

v0.30: --backend posix-local (default) executes grok directly via runner.posix_local after
all gates; --backend wsl-exe stays refused for POSIX-read sources. Historical v0.29 notes: Planning reads the selected files, copies them through the real
snapshot reader/pipe/receiver into a private staging dir, packs them, computes a keyed
consent token, then deletes the staging dir. No model call, no network, nothing stored
except the private consent key (created once, 0600).

--execute is validated (both gates + exact token) and then refused: the source must be read
on POSIX (inside WSL), while the protected ask backend runs from Windows via wsl.exe; no
in-WSL runner ships yet, so no executable path is faked.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from .context_pack import ContextError, consent_fingerprint, load_or_create_consent_key, pack_context

ASK_CTX_PLACEHOLDER_CWD = "/tmp/gbbb-ask-CREATEDONEXECUTE"
LOCAL_PLACEHOLDER_CWD = "/tmp/gbbb-ask-0000000000000000"


def _out(obj: dict, code: int) -> int:
    json.dump(obj, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return code


def _err(code: str, msg: str, **extra) -> int:
    return _out({"error": {"code": code, "message": msg, **extra}}, 2)


def _stage_and_pack(source: str, files: list[str], question: str, key: bytes):
    from . import snapshot_posix as S
    staging = tempfile.mkdtemp(prefix="gbbb-ctx-stage-")
    removed = False
    try:
        os.chmod(staging, 0o700)
        rep = S.read_selection(source, list(files))
        missing = sorted(set(files) - {r for r, _ in rep.files})
        if missing:
            raise ContextError("selection_not_readable",
                               "a selected file was skipped by the snapshot safety rules",
                               skipped=len(missing))
        r, w = os.pipe()
        errs: list[BaseException] = []

        def send():
            try:
                S.send_report(rep, w, deadline_s=30)
            except BaseException as e:  # reported below
                errs.append(e)
            finally:
                os.close(w)
        t = threading.Thread(target=send)
        t.start()
        try:
            res = S.receive_snapshot(r, staging, deadline_s=30)
        finally:
            t.join()
            os.close(r)
        if errs:
            raise errs[0]
        return pack_context(res.snapshot_dir, list(files), question, consent_key=key), rep
    finally:
        shutil.rmtree(staging, ignore_errors=True)  # fd-based on Linux; own fresh dir only
        removed = not os.path.lexists(staging)
        _stage_and_pack.last_removed = removed  # type: ignore[attr-defined]


def cmd_ask_context(args) -> int:
    from .model_select import ModelSelectionError, load_settings, resolve_model
    from .runner.plan import PlanError, build_wsl_grok_ask_plan
    from .snapshot_posix import SnapshotError
    from .state.paths import user_config_root

    if os.name != "posix" or sys.platform == "win32":
        return _err("posix_source_only", "ask-context reads sources only on POSIX (e.g. inside WSL); "
                    "Windows-side sources fail closed (no Windows reader)")
    if args.backend == "posix-local" and (args.wsl_distro or args.wsl_user):
        return _err("backend_args_conflict", "--wsl-distro/--wsl-user apply only to --backend wsl-exe")
    if args.execute and not args.allow_model_call:
        return _err("model_call_gated", "real call requires --allow-model-call")
    if args.execute and not args.confirm_send:
        return _err("send_not_confirmed", "real call requires --confirm-send <token from the dry-run>")
    try:
        settings = load_settings(Path(args.model_settings) if args.model_settings else None)
        choice = resolve_model(args.model, settings, use_preference=not args.no_model_preference)
    except ModelSelectionError as exc:
        return _err(exc.code, exc.message)
    if choice.model_id is None:
        return _err("model_required", "ask-context needs an explicit model (--model or owner preference); "
                    "no implicit Grok default")
    if not choice.expect_usage_keys or choice.kind == "unregistered":
        return _err("model_identity_required", "model must be a registered alias with an expected usage key")
    timeout = args.timeout if args.timeout is not None else (choice.default_timeout_sec or 180.0)
    key_path = args.consent_key or str(user_config_root() / "consent.key")
    try:
        key, key_created = load_or_create_consent_key(key_path)
        packed, rep = _stage_and_pack(args.source, args.file, args.question, key)
    except ContextError as exc:
        return _err(exc.code, exc.message, **exc.info)
    except SnapshotError as exc:
        return _err(exc.code, exc.message)
    except OSError as exc:
        return _err("io_error", f"unexpected {type(exc).__name__} (details withheld: may contain paths)")
    staging_removed = getattr(_stage_and_pack, "last_removed", False)
    from .runner import posix_local as PL
    backend = args.backend
    if backend == "wsl-exe":
        if not (args.wsl_distro and args.wsl_user):
            return _err("wsl_target_required", "--backend wsl-exe needs --wsl-distro and --wsl-user")
        try:
            plan = build_wsl_grok_ask_plan(
                task_id="ask-context", distro=args.wsl_distro, user=args.wsl_user,
                grok_path=args.executable, cwd=ASK_CTX_PLACEHOLDER_CWD, prompt=packed.prompt,
                max_turns=args.max_turns, timeout_sec=timeout, model=choice)
        except PlanError as exc:
            return _err(exc.code, exc.message)
        argv, placeholder, target = plan.argv, ASK_CTX_PLACEHOLDER_CWD, {"distro": plan.wsl_distro, "user": plan.wsl_user}
    else:
        try:
            lp = PL.build_local_plan(grok_path=args.executable, cwd=LOCAL_PLACEHOLDER_CWD, prompt=packed.prompt,
                                     model=choice, max_turns=args.max_turns, timeout_sec=timeout)
        except PL.RunnerError as exc:
            return _err(exc.code, exc.message)
        argv, placeholder, target = lp.argv, LOCAL_PLACEHOLDER_CWD, {}

    def _shape(av, ph):
        return [("<PROMPT>" if a == packed.prompt else "<CWD>" if a == ph else a) for a in av]
    shape = _shape(argv, placeholder)
    binding = {"backend": backend, **target, "executable": args.executable,
               "executable_realpath": os.path.realpath(args.executable), "model_id": choice.model_id,
               "model_kind": choice.kind, "model_source": choice.source,
               "expect_usage_keys": list(choice.expect_usage_keys), "max_turns": args.max_turns,
               "timeout_sec": timeout, "selection": list(args.file),
               "argv_shape_sha256": hashlib.sha256(json.dumps(shape).encode()).hexdigest()}
    token = consent_fingerprint(key, packed, binding)
    if args.execute:
        if not hmac.compare_digest(args.confirm_send, token):
            return _err("consent_mismatch", "token does not match the current files/model/backend/flags; "
                        "re-run the dry-run and review again")
        if backend == "wsl-exe":
            return _err("execute_unsupported_here",
                        "consent verified, but a POSIX-read source cannot use the Windows wsl.exe backend; "
                        "use --backend posix-local inside WSL. Nothing was sent.")
        try:
            PL.validate_executable(args.executable)
            cwd = PL.make_private_cwd()
        except PL.RunnerError as exc:
            return _err(exc.code, exc.message)
        try:
            real = PL.build_local_plan(grok_path=args.executable, cwd=cwd, prompt=packed.prompt,
                                       model=choice, max_turns=args.max_turns, timeout_sec=timeout)
        except PL.RunnerError as exc:
            PL._cleanup_cwd(cwd)
            return _err(exc.code, exc.message)
        if _shape(real.argv, cwd) != shape:
            PL._cleanup_cwd(cwd)
            return _err("consent_mismatch", "launch argv differs from the confirmed plan")
        res = PL.run_local(real, show_answer=args.show_answer)
        return _out({"mode": "execute", "backend": backend, "model_call": True, "stored": False,
                     "result": res}, 0 if res["status"] == "succeeded" else 3)
    return _out({
        "mode": "dry_run", "model_call": False, "network": False,
        "reads": "only the explicitly selected files (text content) via the snapshot reader",
        "writes": {"staging_dir_created": True, "staging_dir_removed": staging_removed,
                   "consent_key_created": key_created, "db": False, "outbox": False},
        "context": packed.summary(), "snapshot_skipped": rep.skipped,
        "model": {"source": choice.source, "kind": choice.kind, "availability": "unverified"},
        "max_turns": args.max_turns, "timeout_sec": timeout, "tools": "all denied (text-only)",
        "consent_token": token,
        "consent_binds": sorted(["prompt_text", *binding]),
        "backend": backend,
        "execute": ("posix-local: --execute --allow-model-call --confirm-send <token> runs grok directly "
                    "(no wsl.exe); executable is checked only at execute" if backend == "posix-local"
                    else "wsl-exe: refused for POSIX-read sources"),
    }, 0)


def add_parser(sub) -> None:
    p = sub.add_parser("ask-context", help="Dry-run: pack explicitly selected files as text-only ask context")
    p.add_argument("--source", required=True, help="Local POSIX source dir (not a Windows path)")
    p.add_argument("--file", action="append", required=True, help="Relative file path; repeat; order kept")
    p.add_argument("--question", required=True)
    p.add_argument("--backend", choices=["posix-local", "wsl-exe"], default="posix-local")
    p.add_argument("--wsl-distro", default=None)
    p.add_argument("--wsl-user", default=None)
    p.add_argument("--executable", required=True)
    p.add_argument("--model", default=None)
    p.add_argument("--no-model-preference", action="store_true")
    p.add_argument("--model-settings", default=None)
    p.add_argument("--max-turns", type=int, default=1)
    p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--consent-key", default=None, help="Private key file (default: user config dir consent.key)")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--allow-model-call", action="store_true")
    p.add_argument("--confirm-send", default=None)
    p.add_argument("--show-answer", action="store_true", help="Print bounded answer (transient, never stored)")
    p.set_defaults(func=cmd_ask_context)

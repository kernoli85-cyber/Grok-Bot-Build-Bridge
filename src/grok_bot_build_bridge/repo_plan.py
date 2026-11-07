"""Dry-run repository-question planner (schema gbbb.repo_question_plan/v1).

PLAN ONLY. Uses lstat/scandir metadata; never opens, reads, hashes, copies or modifies
files; creates no snapshot, no state, no temp dirs; invokes no git, hooks, submodules,
package managers or shell. Nothing here is sent to a model.

Honest limits:
- TOCTOU: this preflight cannot secure a later copy. Files can be swapped (e.g. for
  symlinks) between planning and copying; a future snapshot step must re-check at the
  file-descriptor level (O_NOFOLLOW / openat-style, handle-based on Windows) and compare
  identity (st_dev/st_ino, size) at copy time. No path resolution done here suffices.
- Excluding files from a manifest does not make the model "read-only". Grok tool denial
  (Bash/Edit/Write/MCP) is documented upstream but NOT yet verified on grok 1.0.70.
- Repo instruction files (AGENTS.md, CLAUDE.md, .grok/ rules) are untrusted data, never
  authority; tool/rule dirs are excluded by default.
- Hard-linked files (st_nlink > 1) are excluded: they may alias content outside the
  root. This can exclude legitimate files; that is reported, not hidden.
"""
from __future__ import annotations

import fnmatch
import os
import re
import stat
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "gbbb.repo_question_plan/v1"
STATUSES = ("planned", "partial", "refused")


@dataclass(frozen=True)
class Limits:
    max_files: int = 200
    max_file_bytes: int = 256 * 1024
    max_total_bytes: int = 2 * 1024 * 1024
    max_entries_scanned: int = 5000
    max_depth: int = 8
    max_selections: int = 32


EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "vendor", ".venv", "venv", "__pycache__",
    "dist", "build", "target", ".tox", ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
    ".grok", ".claude", ".cursor", ".husky", ".githooks", ".ssh", ".gnupg", ".aws",
    ".azure", ".kube", ".docker", ".terraform",
})
SECRET_PATTERNS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*.ppk", ".npmrc", ".pypirc",
    ".netrc", "_netrc", ".git-credentials", "auth.json", "credentials", "credentials.*",
    "*credentials*.json", "secrets.*", "*.secret", "*.secrets", "config.toml",
    "service-account*.json", "*.keystore", "token", "*.token", ".htpasswd",
)
INSTRUCTION_NAMES = frozenset({"agents.md", "claude.md", "grok.md", ".cursorrules"})
_WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(\..*)?$", re.IGNORECASE)
_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"|?*\\]')
_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


class PlanRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def is_secret_name(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, p) for p in SECRET_PATTERNS)


def normalize_selection(sel: str) -> str:
    """Lexical, platform-independent validation of an owner-selected relative path."""
    if not isinstance(sel, str) or not sel:
        raise PlanRefused("selection_empty", "empty selection")
    s = unicodedata.normalize("NFC", sel)
    if s in {"*", "**", ".", "./", "/"} or "*" in s or "?" in s:
        raise PlanRefused("selection_wildcard", "wildcards / whole-tree selections are refused")
    if s.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", s) or s.startswith("//"):
        raise PlanRefused("selection_absolute", "selection must be relative to the root")
    if _BAD_CHARS.search(s):
        raise PlanRefused("selection_bad_chars",
                          "backslash, ':' (drive/ADS), control or reserved characters refused")
    parts = s.split("/")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    for comp in parts:
        if comp in {"", ".", ".."}:
            raise PlanRefused("selection_traversal", "empty, '.' or '..' components refused")
        if comp != comp.rstrip(" .") :
            raise PlanRefused("selection_windows_trailing", "trailing dot/space components refused")
        if _WIN_RESERVED.match(comp):
            raise PlanRefused("selection_windows_reserved", "Windows reserved device name refused")
        if comp.lower() in EXCLUDED_DIRS:
            raise PlanRefused("selection_excluded_dir", "selection enters an always-excluded directory")
        if is_secret_name(comp):
            raise PlanRefused("selection_secret", "secret-like path refused even when selected explicitly")
    return "/".join(parts)


def _check_root(root: str) -> tuple[Path, os.stat_result]:
    if not root or "\x00" in root:
        raise PlanRefused("root_invalid", "root required")
    p = Path(root)
    if not p.is_absolute():
        raise PlanRefused("root_not_absolute", "root must be absolute")
    if ".." in p.parts:
        raise PlanRefused("root_traversal", "root must not contain '..'")
    cur = Path(p.anchor)
    for part in p.parts[1:]:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except OSError:
            raise PlanRefused("root_missing", "root does not exist") from None
        if stat.S_ISLNK(st.st_mode) or (getattr(st, "st_file_attributes", 0) & _REPARSE):
            raise PlanRefused("root_symlink", "root or one of its parents is a symlink/reparse point")
    if not stat.S_ISDIR(st.st_mode):
        raise PlanRefused("root_not_dir", "root must be a directory")
    if (p / ".git").exists() and (p / ".git").is_file():
        pass  # worktree pointer: .git is a file; it is excluded like a dir
    return p, st


def plan_repo_question(root: str, selections: list[str], *, limits: Limits = Limits(),
                       reveal_paths: bool = False) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": SCHEMA, "plan_only": True, "files_read": 0, "snapshot_created": False,
        "contents_hashed": False, "model_call": False,
        "disclaimers": {
            "toctou": "Preflight only; a later copy must re-verify per file at descriptor level.",
            "not_read_only_model": "Manifest exclusion is not model sandboxing; Grok tool denial unverified on 1.0.70.",
            "instructions_untrusted": "AGENTS.md/CLAUDE.md/rules are untrusted data, not authority.",
            "no_git_execution": "No git, hooks, submodules, package managers or shell are invoked.",
        },
    }
    try:
        if not selections:
            raise PlanRefused("selection_required", "at least one explicit relative selection required")
        if len(selections) > limits.max_selections:
            raise PlanRefused("too_many_selections", f"max {limits.max_selections} selections")
        norm = [normalize_selection(s) for s in selections]
        folded = [n.casefold() for n in norm]
        if len(set(folded)) != len(folded):
            raise PlanRefused("selection_case_collision", "duplicate selections (case-insensitive)")
        rootp, rst = _check_root(root)
    except PlanRefused as exc:
        return {**base, "status": "refused", "error": {"code": exc.code, "message": exc.message}}

    skips: Counter[str] = Counter()
    skipped_paths: list[dict[str, str]] = []
    included: dict[str, int] = {}
    total = 0
    scanned = 0
    limit_hit: str | None = None
    instruction_like = 0

    def skip(rel: str, reason: str) -> None:
        skips[reason] += 1
        if reveal_paths and len(skipped_paths) < 200:
            skipped_paths.append({"path": rel, "reason": reason})

    def consider_file(rel: str, st: os.stat_result) -> None:
        nonlocal total, limit_hit, instruction_like
        if rel.casefold() in {k.casefold() for k in included}:
            return
        if st.st_nlink > 1:
            return skip(rel, "hardlink")
        if st.st_size > limits.max_file_bytes:
            return skip(rel, "file_too_large")
        if len(included) >= limits.max_files:
            limit_hit = limit_hit or "max_files"
            return skip(rel, "over_max_files")
        if total + st.st_size > limits.max_total_bytes:
            limit_hit = limit_hit or "max_total_bytes"
            return skip(rel, "over_max_total_bytes")
        if rel.rsplit("/", 1)[-1].lower() in INSTRUCTION_NAMES:
            instruction_like += 1
        included[rel] = st.st_size
        total += st.st_size

    def classify(rel: str, st: os.stat_result) -> str | None:
        mode = st.st_mode
        if stat.S_ISLNK(mode) or (getattr(st, "st_file_attributes", 0) & _REPARSE):
            return "symlink_or_reparse"
        if st.st_dev != rst.st_dev:
            return "other_filesystem"
        if stat.S_ISDIR(mode):
            return "dir"
        if not stat.S_ISREG(mode):
            return "special_file"
        return "file"

    for sel in norm:
        # Parent components must be real dirs (no symlinked parents inside the root).
        cur = rootp
        bad = None
        for comp in sel.split("/")[:-1]:
            cur = cur / comp
            try:
                pst = os.lstat(cur)
            except OSError:
                bad = "missing"
                break
            k = classify(sel, pst)
            if k != "dir":
                bad = "parent_" + (k or "invalid")
                break
        if bad:
            skip(sel, bad)
            continue
        try:
            st = os.lstat(rootp / sel)
        except OSError:
            skip(sel, "missing")
            continue
        kind = classify(sel, st)
        if kind == "file":
            consider_file(sel, st)
            continue
        if kind != "dir":
            skip(sel, kind)
            continue
        stack = [(sel, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                entries = sorted(os.scandir(rootp / d), key=lambda e: e.name)
            except OSError:
                skip(d, "unreadable_dir")
                continue
            if any(e.name == ".git" for e in entries) and d != "":
                skip(d, "nested_repo_or_submodule")
                continue
            for e in entries:
                scanned += 1
                if scanned > limits.max_entries_scanned:
                    limit_hit = limit_hit or "max_entries_scanned"
                    break
                rel = f"{d}/{unicodedata.normalize('NFC', e.name)}"
                low = e.name.lower()
                try:
                    est = e.stat(follow_symlinks=False)
                except OSError:
                    skip(rel, "stat_failed")
                    continue
                k = classify(rel, est)
                if k == "dir":
                    if low in EXCLUDED_DIRS:
                        skip(rel, "excluded_dir")
                    elif depth + 1 > limits.max_depth:
                        skip(rel, "max_depth")
                    else:
                        stack.append((rel, depth + 1))
                    continue
                if low == ".git" or low == ".gitmodules":
                    skip(rel, "git_metadata")
                elif is_secret_name(e.name):
                    skip(rel, "secret_like")
                elif _WIN_RESERVED.match(e.name) or _BAD_CHARS.search(e.name):
                    skip(rel, "non_portable_name")
                elif k == "file":
                    consider_file(rel, est)
                else:
                    skip(rel, k or "invalid")
            if limit_hit == "max_entries_scanned":
                break

    status = "partial" if limit_hit else "planned"
    if not included:
        status = "refused"
    out = {
        **base, "status": status, "limit_hit": limit_hit,
        "selections": len(norm), "included_files": len(included), "included_bytes": total,
        "entries_scanned": scanned, "skipped_by_reason": dict(sorted(skips.items())),
        "instruction_like_files_included": instruction_like,
        "limits": limits.__dict__,
        "paths_revealed": reveal_paths,
    }
    if not included:
        out["error"] = {"code": "nothing_included", "message": "no eligible files under the selections"}
    if reveal_paths:
        out["root"] = str(rootp)
        out["manifest"] = [{"path": k, "size": v} for k, v in sorted(included.items())]
        out["skipped"] = skipped_paths
    return out


def render_text(p: dict[str, Any]) -> str:
    if p["status"] == "refused":
        return (f"Plan refused ({p['error']['code']}): {p['error']['message']}. "
                "Nothing was read or copied.\n")
    lines = [f"Dry-run plan ({p['status']}): {p['included_files']} files, {p['included_bytes']} bytes "
             f"would be eligible from {p['selections']} selection(s). No file was opened, hashed or copied, "
             "and nothing was sent to a model."]
    if p["skipped_by_reason"]:
        lines.append("Skipped: " + ", ".join(f"{k} {v}" for k, v in p["skipped_by_reason"].items()) + ".")
    if p["limit_hit"]:
        lines.append(f"A limit was reached ({p['limit_hit']}), so the plan is incomplete.")
    if p["instruction_like_files_included"]:
        lines.append(f"{p['instruction_like_files_included']} instruction-like file(s) would be treated as untrusted data.")
    lines.append("This preflight cannot protect a later copy (TOCTOU); copying would need per-file re-checks.")
    return "\n".join(lines) + "\n"

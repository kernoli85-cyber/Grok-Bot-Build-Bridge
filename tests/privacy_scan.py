"""Category scanner for accidental private material.

Violations are category + path only. Patterns describe shapes; they do not
list any previously found private value.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from grok_bot_build_bridge.demo_facts import DEMO_GECOS, DEMO_USER

_SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "dist", "build", ".eggs"}
_HOME = re.compile(r"/home/([A-Za-z0-9._-]+)")
_WIN_USER = re.compile(r"Users[/\\]([A-Za-z0-9._-]+)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_TMP = re.compile(r"/tmp/gbbb-(?:ask|smoke|canary|ctx)-([A-Za-z0-9]+)")
_PASSWD = re.compile(r"^[^:\n]+:[^:\n]*:\d+:\d+:([^:\n]*):", re.M)
_TOKENS = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{16,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack_webhook", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("discord_webhook", re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/")),
)
_EMAIL_DOMAINS = {"example.com", "example.org", "example.net"}
_HOME_USERS = {DEMO_USER, "bad", "u", "x"}
_WIN_USERS = {DEMO_USER, "x"}
_GECOS = {"root", "nobody", DEMO_GECOS, ""}
_TMP_OK = {"CREATEDONEXECUTE", "placeholder0", "AbC123xyz", "Abcdefgh1234", "abcdefgh", "a"}


def _tmp_ok(suffix: str) -> bool:
    if suffix in _TMP_OK:
        return True
    if re.fullmatch(r"[0-9a-f]{16}", suffix):
        return True
    upper = suffix.upper()
    if any(token in upper for token in ("PLACE", "MOCK", "DIR")):
        return True
    if set(suffix) <= {"X"} or set(suffix) <= {"0"}:
        return True
    return False


def violations(text: str, rel: str) -> list[str]:
    found: list[str] = []
    for user in _HOME.findall(text):
        if user not in _HOME_USERS:
            found.append(f"home_account@{rel}")
            break
    for user in _WIN_USER.findall(text):
        if user not in _WIN_USERS:
            found.append(f"windows_account@{rel}")
            break
    for domain in _EMAIL.findall(text):
        if domain.lower() not in _EMAIL_DOMAINS:
            found.append(f"email@{rel}")
            break
    for suffix in _TMP.findall(text):
        if not _tmp_ok(suffix):
            found.append(f"concrete_temp_dir@{rel}")
            break
    if "passwd" in Path(rel).name:
        for gecos in _PASSWD.findall(text):
            if gecos not in _GECOS:
                found.append(f"passwd_gecos@{rel}")
                break
    for name, pattern in _TOKENS:
        if pattern.search(text):
            found.append(f"{name}@{rel}")
    return found


def scan_tree(root: Path) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".whl", ".gz", ".pyc"}:
            continue
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            continue
        hits.extend(violations(data.decode("utf-8", "replace"), str(path.relative_to(root))))
    return hits


def scan_zip(path: Path) -> list[str]:
    hits: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            data = archive.read(info)
            if b"\x00" in data[:4096]:
                continue
            hits.extend(violations(data.decode("utf-8", "replace"), info.filename))
    return hits


def scan_tar(path: Path) -> list[str]:
    import tarfile
    hits: list[str] = []
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            data = extracted.read() if extracted is not None else b""
            if b"\x00" in data[:4096]:
                continue
            hits.extend(violations(data.decode("utf-8", "replace"), member.name))
    return hits

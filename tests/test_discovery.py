from __future__ import annotations

from pathlib import Path

from grok_bot_build_bridge.discovery import (
    classify_discovery,
    nonroot_ready,
    parse_grok_version,
    parse_wsl_list_verbose,
)
from grok_bot_build_bridge.models import BackendState

FIX = Path(__file__).parent / "fixtures"


def test_parse_grok_version():
    text = (FIX / "grok_version_ok.txt").read_text(encoding="utf-8")
    assert parse_grok_version(text) == "1.0.70"
    assert parse_grok_version("nope") is None


def test_parse_wsl_list_running():
    text = (FIX / "wsl_list_v_running.txt").read_text(encoding="utf-8")
    rows = parse_wsl_list_verbose(text)
    assert len(rows) == 1
    assert rows[0]["name"] == "Ubuntu"
    assert rows[0]["state"] == "Running"
    assert rows[0]["default"] is True


def test_root_user_never_ready():
    c = nonroot_ready(
        "Ubuntu", "root", 0, "Running", "/usr/bin/grok", "grok 1.0.70"
    )
    assert c.state == BackendState.NOT_READY
    assert "root_or_system_user_not_ready" in c.notes


def test_stopped_distro_asks():
    c = nonroot_ready(
        "Ubuntu", "okern", 1000, "Stopped", "/home/okern/.local/bin/grok", "grok 1.0.70"
    )
    assert c.state == BackendState.STOPPED


def test_wsl_only_matrix():
    cand = nonroot_ready(
        "Ubuntu",
        "okern",
        1000,
        "Running",
        "/home/okern/.local/bin/grok",
        (FIX / "grok_version_ok.txt").read_text(encoding="utf-8"),
    )
    report = classify_discovery(
        native_path=None,
        native_version_text=None,
        wsl_candidates=(cand,),
    )
    assert report.kind == "wsl_only"
    assert report.default_backend == "wsl"
    assert report.ready_backends() == frozenset({"wsl"})


def test_native_only_matrix():
    report = classify_discovery(
        native_path=r"C:\Users\x\AppData\Local\Programs\grok\grok.exe",
        native_version_text="grok 1.0.70",
        wsl_candidates=(),
    )
    assert report.kind == "native_only"
    assert report.default_backend == "native"


def test_both_requires_explicit_default():
    cand = nonroot_ready(
        "Ubuntu", "okern", 1000, "Running", "/home/okern/.local/bin/grok", "grok 1.0.70"
    )
    report = classify_discovery(
        native_path=r"C:\grok\grok.exe",
        native_version_text="grok 1.0.70",
        wsl_candidates=(cand,),
    )
    assert report.kind == "both"
    assert report.default_backend is None


def test_none_matrix():
    report = classify_discovery(
        native_path=None, native_version_text=None, wsl_candidates=()
    )
    assert report.kind == "none"
    assert report.default_backend is None

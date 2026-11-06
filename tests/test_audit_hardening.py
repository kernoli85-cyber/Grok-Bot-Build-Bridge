"""Regression tests for audit-hardening (Slice 11)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from grok_bot_build_bridge.preferences import (
    OperatorPreferences,
    PreferenceError,
    load_preferences,
    save_preferences,
)
from grok_bot_build_bridge.runner.plan import PlanError, build_native_invocation
from grok_bot_build_bridge.safe_exec import DEFAULT_MAX_STDIN_BYTES, run_argv
from grok_bot_build_bridge.state.clock import Clock, Jitter
from grok_bot_build_bridge.state.store import JobStore, StoreError
from grok_bot_build_bridge.state.types import JobStatus
from grok_bot_build_bridge.transport.webhook import WebhookConfig, WebhookTransport, validate_webhook_url


def _store(tmp_path: Path, t0: float = 1_000.0) -> JobStore:
    clock = Clock()
    clock.set(t0)
    return JobStore(tmp_path / "state.sqlite", clock=clock, jitter=Jitter(seed=42))


def test_webhook_blocks_loopback_and_private_literals():
    for bad in (
        "https://127.0.0.1/hook",
        "https://localhost/hook",
        "https://10.0.0.1/x",
        "https://192.168.1.1/x",
        "https://169.254.169.254/latest/meta-data/",
        "https://metadata.google.internal/",
    ):
        with pytest.raises(ValueError, match="url_host_forbidden"):
            validate_webhook_url(bad)
    validate_webhook_url("https://hooks.example.com/path")


def test_webhook_opener_disables_env_proxy(monkeypatch):
    """Empty ProxyHandler suppresses urllib's default env-proxy handler."""
    import urllib.request
    from urllib.request import ProxyHandler

    monkeypatch.setenv("HTTPS_PROXY", "http://evil-proxy.example:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://evil-proxy.example:8080")
    monkeypatch.setenv("https_proxy", "http://evil-proxy.example:8080")
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://evil-proxy.example:8080", "http": "http://evil-proxy.example:8080"},
    )
    cfg = WebhookConfig(url="https://hooks.example.com/hook", bearer_token="tok")
    transport = WebhookTransport(cfg)
    handlers = transport._opener.handlers  # type: ignore[attr-defined]
    # Must not register a ProxyHandler that would forward to the env proxy
    for h in handlers:
        if isinstance(h, ProxyHandler):
            assert h.proxies == {}
    # Default ProxyHandler (which would call getproxies) must be absent/suppressed
    assert not any(isinstance(h, ProxyHandler) and bool(getattr(h, "proxies", None)) for h in handlers)


def test_cmd_bat_executable_refused(tmp_path: Path):
    cmd = tmp_path / "grok.cmd"
    cmd.write_text("@echo off\n", encoding="utf-8")
    with pytest.raises(PlanError) as ei:
        build_native_invocation(str(cmd), ["--cwd", "C:\\x"])
    assert ei.value.code == "cmd_bat_refused"


def test_argv_metachar_refused_on_native(tmp_path: Path):
    exe = tmp_path / "grok.exe"
    exe.write_bytes(b"MZ")
    with pytest.raises(PlanError) as ei:
        build_native_invocation(str(exe), ["--cwd", "C:\\x & calc.exe"])
    assert ei.value.code == "argv_metachar"


def test_stdin_bound_in_safe_exec():
    huge = b"x" * (DEFAULT_MAX_STDIN_BYTES + 1)
    r = run_argv(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin_bytes=huge,
        timeout_sec=2,
    )
    assert r.error == "stdin_too_large"


def test_child_env_refuses_secret_keys_and_does_not_forward_full_parent(monkeypatch):
    monkeypatch.setenv("LEAKED_SECRET_TOKEN", "should-not-reach-child")
    r = run_argv(
        [
            sys.executable,
            "-c",
            "import os,sys; sys.exit(0 if 'LEAKED_SECRET_TOKEN' not in os.environ else 2)",
        ],
        env={"TASK_ID": "t1"},
        timeout_sec=5,
    )
    assert r.returncode == 0, (r.error, r.stderr)
    r2 = run_argv(
        [sys.executable, "-c", "pass"],
        env={"API_TOKEN": "nope"},
        timeout_sec=2,
    )
    assert r2.error == "env_key_forbidden"


def test_prefs_refuse_symlink(tmp_path: Path):
    if sys.platform.startswith("win"):
        pytest.skip("symlink privileges vary on Windows CI")
    real = tmp_path / "real.json"
    link = tmp_path / "link.json"
    real.write_text("{}", encoding="utf-8")
    link.symlink_to(real)
    with pytest.raises(PreferenceError) as ei:
        load_preferences(link)
    assert ei.value.code == "symlink_refused"
    prefs = OperatorPreferences(
        schema_version=1,
        desired="native",
        default_backend="native",
        discovery_kind_at_save="native_only",
        notes=(),
    )
    other = tmp_path / "other.json"
    other.write_text("x", encoding="utf-8")
    target = tmp_path / "prefs.json"
    target.symlink_to(other)
    with pytest.raises(PreferenceError) as ei2:
        save_preferences(prefs, target, overwrite=True)
    assert ei2.value.code == "symlink_refused"


def test_claim_expect_status_queued_race(tmp_path: Path):
    store = _store(tmp_path)
    store.enqueue_job(task_id="t-race", backend="native", cwd=str(tmp_path))
    a = store.claim_job("t-race", owner="w1", lease_sec=30)
    assert a.status == JobStatus.CLAIMED
    with pytest.raises(StoreError) as ei:
        store.claim_job("t-race", owner="w2", lease_sec=30)
    assert ei.value.code in {"not_claimable", "race_lost"}


def test_cancel_is_terminal_no_auto_requeue(tmp_path: Path):
    store = _store(tmp_path)
    store.enqueue_job(task_id="t-c", backend="native", cwd=str(tmp_path))
    store.claim_job("t-c", owner="w1", lease_sec=30)
    cancelled = store.cancel("t-c")
    assert cancelled.status == JobStatus.CANCELLED
    with pytest.raises(StoreError):
        store.claim_job("t-c", owner="w2", lease_sec=30)
    with pytest.raises(StoreError) as ei:
        store.explicit_retry("t-c")
    assert ei.value.code == "retry_not_allowed"


def test_outbox_cancel_event_separate_from_job_reexec(tmp_path: Path):
    store = _store(tmp_path)
    store.enqueue_job(task_id="t-o", backend="native", cwd=str(tmp_path))
    store.cancel("t-o")
    events = store.list_outbox()
    assert any(e.event_type == "job.cancelled" for e in events)
    job = store.get_job("t-o")
    assert job is not None and job.status == JobStatus.CANCELLED


def test_docs_do_not_claim_cursor_plugin_equals_grok_bot():
    text = (Path(__file__).resolve().parents[1] / "docs" / "DISTRIBUTION.md").read_text(
        encoding="utf-8"
    )
    assert "Do not assume" in text or "do not assume" in text.lower()
    assert "unverified" in text.lower()
    plugin_readme = (
        Path(__file__).resolve().parents[1] / "drafts" / "plugin" / "README.md"
    ).read_text(encoding="utf-8")
    assert "same approved Cursor marketplace plugins" not in plugin_readme

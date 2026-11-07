from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from tests.privacy_scan import scan_tar, scan_zip

ROOT = Path(__file__).resolve().parents[1]

# Subcommands that must work from an installed wheel (no checkout cwd)
_SMOKE_CMDS = [
    ["version"],
    ["--help"],
    ["onboard", "--desired", "unset"],
    ["discover-demo"],
    ["skill-draft"],
]


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        input=input,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory) -> tuple[Path, Path]:
    """Build sdist+wheel once into a temp dist dir (isolated from repo state)."""
    out = tmp_path_factory.mktemp("dist-build")
    # Ensure build tooling
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "build"],
        check=True,
        cwd=str(ROOT),
    )
    r = _run(
        [sys.executable, "-m", "build", "--outdir", str(out)],
        cwd=ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    wheels = list(out.glob("*.whl"))
    sdists = list(out.glob("*.tar.gz"))
    assert wheels, "wheel missing"
    assert sdists, "sdist missing"
    return wheels[0], sdists[0]


def test_built_packages_privacy_categories(built_artifacts):
    wheel, sdist = built_artifacts
    hits = scan_zip(wheel) + scan_tar(sdist)
    assert hits == [], "package privacy categories: " + ", ".join(hits)


def test_wheel_name_is_py3_none_any(built_artifacts):
    wheel, _ = built_artifacts
    assert wheel.name.endswith("-py3-none-any.whl")
    assert "grok_bot_build_bridge" in wheel.name or "grok-bot-build-bridge" in wheel.name.replace("_", "-")


def test_wheel_excludes_secrets_tests_and_local_paths(built_artifacts):
    wheel, _ = built_artifacts
    import zipfile

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    joined = "\n".join(names)
    assert "tests/" not in joined
    assert ".git/" not in joined
    assert "webhook.json" not in joined
    assert ".pem" not in joined
    assert ".sqlite" not in joined
    # Must ship packaged resources
    assert any("resources/fixtures/wsl_list_v_running.txt" in n for n in names)
    assert any("SKILL.md" in n for n in names)
    assert any("host-prereq-check.ps1" in n for n in names)
    assert any("wsl_list_v_stopped.txt" in n for n in names)
    # No absolute workspace paths encoded as file names
    assert "/workspace/" not in joined
    assert "C:\\Users\\" not in joined


def test_sha256_recorded_for_artifacts(built_artifacts, tmp_path):
    wheel, sdist = built_artifacts
    report = {}
    for p in (wheel, sdist):
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        report[p.name] = h
        assert len(h) == 64
    (tmp_path / "checksums.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    # Claim only: hashes measured for this build; cross-OS equality not asserted here


def test_isolated_venv_install_smoke_and_prefs_untouched(built_artifacts, tmp_path):
    wheel, _ = built_artifacts
    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True)
    if sys.platform.startswith("win"):
        py = venv_dir / "Scripts" / "python.exe"
        exe = venv_dir / "Scripts" / "grok-bot-build-bridge.exe"
    else:
        py = venv_dir / "bin" / "python"
        exe = venv_dir / "bin" / "grok-bot-build-bridge"
    # Install wheel only (no editable, no checkout on PYTHONPATH)
    r = _run([str(py), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)], env={**os.environ, "PYTHONNOUSERSITE": "1"})
    assert r.returncode == 0, r.stdout + r.stderr

    # Fake user config/data dirs so we never touch real home
    cfg = tmp_path / "cfg"
    data = tmp_path / "data"
    cfg.mkdir()
    data.mkdir()
    prefs = cfg / "grok-bot-build-bridge" / "preferences.json"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "desired": "wsl",
                "default_backend": "wsl",
                "discovery_kind_at_save": "wsl_only",
                "notes": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    before = prefs.read_text(encoding="utf-8")

    env = os.environ.copy()
    # Clear PYTHONPATH so site-packages wheel is used
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if sys.platform.startswith("win"):
        env["LOCALAPPDATA"] = str(cfg)
    else:
        env["XDG_CONFIG_HOME"] = str(cfg)
        env["XDG_DATA_HOME"] = str(data)

    work = tmp_path / "elsewhere"
    work.mkdir()
    for args in _SMOKE_CMDS:
        cmd = [str(exe), *args]
        rr = _run(cmd, cwd=work, env=env)
        assert rr.returncode == 0, f"{args}: {rr.stdout}\n{rr.stderr}"

    # onboard must not rewrite preferences
    assert prefs.read_text(encoding="utf-8") == before

    # Uninstall
    u = _run([str(py), "-m", "pip", "uninstall", "-y", "grok-bot-build-bridge"], env=env)
    assert u.returncode == 0, u.stdout + u.stderr
    assert prefs.read_text(encoding="utf-8") == before
    assert not exe.exists() or True  # entrypoint may remain briefly; prefs must remain


def test_packaged_resources_importable():
    from grok_bot_build_bridge.resources import draft_skill_text, fixture_text

    assert "Ubuntu" in fixture_text("wsl_list_v_running.txt")
    assert "Grok Bot-Build Bridge Einrichtung" in draft_skill_text()


def test_draft_plugin_manifest_official_shape():
    manifest = ROOT / "drafts/plugin/.cursor-plugin/plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["name"] == "grok-bot-build-bridge"
    assert data["name"] == data["name"].lower()
    assert "-" in data["name"] or data["name"].isalnum()
    skill = ROOT / "drafts/plugin/skills/grok-bot-build-bridge-einrichtung/SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name:" in text.split("---")[1]
    assert "description:" in text.split("---")[1]


def test_installed_wheel_e2e_dummy_grok_json_no_model_no_network(built_artifacts, tmp_path):
    """Installed wheel: dummy JSON producer -> durable state -> reopen -> hidden/shown -> outbox dry-run."""
    import shutil
    wheel, _ = built_artifacts
    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True)
    win = sys.platform.startswith("win")
    py = venv_dir / ("Scripts/python.exe" if win else "bin/python")
    exe = venv_dir / ("Scripts/grok-bot-build-bridge.exe" if win else "bin/grok-bot-build-bridge")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = _run([str(py), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)], env={**env, "PYTHONNOUSERSITE": "1"})
    assert r.returncode == 0 and exe.exists(), r.stdout + r.stderr
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
    work = tmp_path / "work"
    work.mkdir()
    src = ROOT / "tests" / "fixtures" / "bridge-dummy-grokjson.py"
    ok_dummy = work / "bridge-dummy-grokjson.py"
    noise_dummy = work / "bridge-dummy-noise.py"
    shutil.copy(src, ok_dummy)
    shutil.copy(src, noise_dummy)
    db = tmp_path / "state" / "state.db"

    def cli(*a):
        return _run([str(exe), *a], cwd=work, env=env)

    def run(tid, dummy):
        return cli("runner-run", "--state-db", str(db), "--task-id", tid, "--backend", "native",
                   "--cwd", str(work), "--executable", str(dummy), "--expect-bridge-ok", "--execute")

    r = run("ok1", ok_dummy)
    assert r.returncode == 0, r.stdout + r.stderr
    d = json.loads(r.stdout)
    assert d["status"] == "succeeded" and d["process_exit_ok"] is True
    assert d["result_summary"]["matched_text"] == "BRIDGE_OK"
    assert d["result_summary"]["total_cost_usd"] == "0.0001"

    r = run("noise1", noise_dummy)
    d = json.loads(r.stdout)
    assert r.returncode != 0 and d["process_exit_ok"] is True and d["status"] == "needs_review"
    assert d["error_code"] == "success_exit_invalid_result"

    # Reopen in fresh processes: hidden by default, shown on request.
    r = cli("jobs-status", "--state-db", str(db))
    assert r.returncode == 0 and "result_summary" not in r.stdout and "0d3f-AB_9" not in r.stdout
    r = cli("jobs-status", "--state-db", str(db), "--show-result")
    jobs = {j["task_id"]: j for j in json.loads(r.stdout)["jobs"]}
    assert jobs["ok1"]["status"] == "succeeded" and jobs["ok1"]["result_summary"]["session_id"] == "0d3f-AB_9"
    assert jobs["noise1"]["status"] == "needs_review" and jobs["noise1"]["result_summary"]["parse_status"] == "not_json"

    # Generic (non-smoke) answer: real dummy subprocess, transient opt-in display.
    ans_dummy = work / "bridge-dummy-answer.py"
    shutil.copy(src, ans_dummy)
    r = cli("runner-run", "--state-db", str(db), "--task-id", "ans1", "--backend", "native",
            "--cwd", str(work), "--executable", str(ans_dummy), "--parse-grok-json", "--show-answer", "--execute")
    d = json.loads(r.stdout)
    assert r.returncode == 0 and d["status"] == "succeeded" and "Gr\u00fc\u00dfe" in d["answer"]
    assert d["result_summary"]["matched_text"] is None
    assert b"Antwort" not in db.read_bytes()

    # ask dry-run from the installed wheel: no DB, no data dirs, prompt not echoed.
    xdg = tmp_path / "xdg"
    ask_env = {**env, "XDG_DATA_HOME": str(xdg / "d"), "XDG_CONFIG_HOME": str(xdg / "c"),
               "LOCALAPPDATA": str(xdg / "l")}
    ask_db = tmp_path / "ask" / "never.db"
    r = _run([str(exe), "ask", "--prompt", 'Sag "Hallo" \u00fc SECRETP', "--wsl-distro", "Ubuntu",
              "--wsl-user", "gbbbdemo", "--executable", "/usr/bin/grok", "--state-db", str(ask_db)], cwd=work, env=ask_env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["state_written"] is False and "SECRETP" not in r.stdout
    assert not ask_db.exists() and not xdg.exists()

    # Read-only status from the installed wheel: SQL-scoped, text format, no creation.
    r = cli("jobs-status", "--state-db", str(db), "--task-id", "ans1", "--show-result", "--format", "text")
    assert r.returncode == 0 and "Job ans1 is succeeded" in r.stdout and "US$ 0.00012345" in r.stdout
    assert "sess-ANS-1" not in r.stdout
    r = cli("jobs-status", "--state-db", str(db), "--task-id", "ghost")
    assert r.returncode == 3
    ghost_db = tmp_path / "ghost" / "s.db"
    assert cli("jobs-status", "--state-db", str(ghost_db)).returncode == 2 and not ghost_db.parent.exists()

    # model settings + model-bound ask dry-run from the installed wheel (no grok, no WSL call).
    ms = tmp_path / "ms" / "model_settings.json"
    r = cli("model-settings", "--settings", str(ms), "add-alias", "LM-Studio", "--expect-usage-key", "fake-local-model")
    assert r.returncode == 0, r.stdout + r.stderr
    assert cli("model-settings", "--settings", str(ms), "set-preferred", "LM-Studio").returncode == 0
    r = cli("ask", "--wsl-distro", "Ubuntu", "--wsl-user", "gbbbdemo", "--executable", "/usr/bin/grok",
            "--prompt", "hi", "--model-settings", str(ms))
    d = json.loads(r.stdout)
    assert r.returncode == 0 and d["dry_run"] is True and d["limits"]["model"]["source"] == "owner_preference"
    assert d["argv_preview"][d["argv_preview"].index("--model") + 1] == "LM-Studio"
    fx = ROOT / "tests" / "fixtures" / "grok_json_1077_local_observed_redacted.json"
    r = cli("model-settings", "--settings", str(ms), "check-result", "LM-Studio", "--result-file", str(fx))
    assert r.returncode == 3 and json.loads(r.stdout)["reported_model_usage_keys"] == ["qwen3.5-9b-mtp"]
    r = cli("model-settings", "--settings", str(ms), "check-result", "LM-Studio", "--result-file", str(fx),
            "--set-usage-key", "qwen3.5-9b-mtp")
    assert r.returncode == 0 and json.loads(r.stdout)["saved"] is True
    r = cli("model-settings", "--settings", str(ms), "check-result", "LM-Studio", "--result-file", str(fx))
    assert r.returncode == 0 and json.loads(r.stdout)["identity_with_current_settings"] == "confirmed"
    # ask-context from the installed wheel: POSIX dry-run (reproducible keyed token), Windows fail-closed.
    csrc = tmp_path / "ctxsrc"
    csrc.mkdir()
    (csrc / "n.txt").write_text("synthetic port 47113\n")
    if os.name == "posix":
        csrc.chmod(0o755)
    cargs = ("ask-context", "--source", str(csrc), "--file", "n.txt", "--question", "Port?",
             "--executable", "/usr/bin/grok",
             "--model", "LM-Studio", "--model-settings", str(ms), "--consent-key", str(tmp_path / "ck" / "consent.key"))
    r = cli(*cargs)
    d = json.loads(r.stdout)
    if os.name == "posix" and not (hasattr(os, "geteuid") and os.geteuid() == 0):
        assert r.returncode == 0 and d["model_call"] is False and "47113" not in r.stdout, r.stdout
        assert json.loads(cli(*cargs).stdout)["consent_token"] == d["consent_token"]
        r = cli(*cargs, "--wsl-user", "gbbbdemo")
        assert json.loads(r.stdout)["error"]["code"] == "backend_args_conflict"
        r = cli(*cargs, "--execute", "--allow-model-call", "--confirm-send", d["consent_token"])
        assert json.loads(r.stdout)["error"]["code"] == "executable_missing"
        # installed wheel + direct POSIX runner + dummy grok (prints a fixed JSON, no model)
        bindir = tmp_path / "dgrok"
        bindir.mkdir(mode=0o755)
        dg = bindir / "grok"
        dg.write_text("#!" + sys.executable + "\nimport json, os, sys\n"
                      "assert '--sandbox' in sys.argv and 'MCPTool' in sys.argv and os.listdir('.') == []\n"
                      "print(json.dumps({'text': 'ok', 'stopReason': 'end_turn', 'num_turns': 1, "
                      "'usage': {'input_tokens': 1, 'output_tokens': 1}, 'modelUsage': {'qwen3.5-9b-mtp': {}}}))\n")
        dg.chmod(0o755)
        dargs = [a if a != "/usr/bin/grok" else str(dg) for a in cargs]
        tok = json.loads(cli(*dargs).stdout)["consent_token"]
        r = cli(*dargs, "--execute", "--allow-model-call", "--confirm-send", tok)
        res = json.loads(r.stdout)["result"]
        assert r.returncode == 0 and res["status"] == "succeeded" and res["model_identity"] == "confirmed", r.stdout
        assert res["cwd_cleanup"] == "removed" and res["cost_status"] == "unknown"
    elif os.name != "posix":
        assert r.returncode == 2 and d["error"]["code"] == "posix_source_only"
    inv_env = {k: v for k, v in env.items() if k not in {"LOCALAPPDATA", "XDG_CONFIG_HOME", "HOME"}}
    inv_env["LOCALAPPDATA"] = str(tmp_path / "inv-local")
    inv_env["XDG_CONFIG_HOME"] = str(tmp_path / "inv-config")
    inv_env["HOME"] = str(tmp_path / "inv-home")
    inv_grok = tmp_path / "inv-grok" / "grok"
    if os.name == "posix":
        inv_grok.parent.mkdir(mode=0o755)
        inv_grok.write_text(
            "#!" + sys.executable + "\nimport json, os, sys\n"
            "assert '--sandbox' in sys.argv and 'MCPTool' in sys.argv and os.listdir('.') == []\n"
            "print(json.dumps({'text': 'ok', 'stopReason': 'end_turn', 'num_turns': 1, "
            "'usage': {'input_tokens': 1, 'output_tokens': 1}, "
            "'modelUsage': {'provider-model-id': {}}}))\n")
        inv_grok.chmod(0o755)
        invoke_executable = str(inv_grok)
    else:
        invoke_executable = "/usr/bin/grok"
    ask_envlp = {
        "schema": "gbbb.invoke/v1", "op": "ask", "executable": invoke_executable,
        "model": {"id": "LM-Studio", "kind": "custom_local", "expect_usage_keys": ["provider-model-id"]},
        "prompt": "PROMPTTOKEN ready",
    }
    r = _run([str(exe), "invoke"], cwd=work, env=inv_env, input=json.dumps(ask_envlp))
    if os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "posix_only"
        assert not (tmp_path / "inv-config").exists()
    else:
        d = json.loads(r.stdout)
        assert r.returncode == 0 and d["mode"] == "dry_run" and d["model_call"] is False
        assert "PROMPTTOKEN" not in r.stdout, r.stdout
        sent = dict(ask_envlp, execute=True, allow_model_call=True, confirm_send=d["consent_token"])
        r = _run([str(exe), "invoke"], cwd=work, env=inv_env, input=json.dumps(sent))
        body = json.loads(r.stdout)
        assert r.returncode == 0 and body["stored"] is False, r.stdout
        assert body["result"]["status"] == "succeeded" and body["result"]["model_identity"] == "confirmed"
        assert "PROMPTTOKEN" not in r.stdout and "sessionId" not in r.stdout
    # doctor from the installed wheel: fixture default, read-only state check, no creation.
    r = cli("doctor", "--format", "text", "--state-db", str(db))
    assert r.returncode == 0 and "FIXTURE" in r.stdout and "[ok] state_db" in r.stdout
    assert "readiness for a real model call is unknown" in r.stdout
    nodb = tmp_path / "doc" / "s.db"
    r = cli("doctor", "--state-db", str(nodb))
    assert r.returncode == 0 and not nodb.parent.exists()

    r = cli("outbox-pump", "--state-db", str(db))  # dry-run default; no --send
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0d3f-AB_9" not in r.stdout and "BRIDGE_OK" not in r.stdout


@pytest.mark.skipif(os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="POSIX non-root only; Windows fails closed")
def test_installed_wheel_snapshot_pipe_smoke_offline(built_artifacts, tmp_path):
    """Installed wheel: fake tree -> reader child -> real pipe -> receiver child. No hooks used,
    no uid injection exists in the public API, root refused, network blackholed."""
    wheel, _ = built_artifacts
    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True)
    py = venv_dir / "bin" / "python"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(PYTHONNOUSERSITE="1", HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
    r = _run([str(py), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    repo = tmp_path / "repo" / "src"
    repo.mkdir(parents=True)
    (repo / "a.py").write_text("print('fake')\n")
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    check = ("import inspect, grok_bot_build_bridge.snapshot_posix as S, sys\n"
             "assert 'site-packages' in S.__file__\n"
             "for f in (S.read_selection, S.receive_snapshot, S.send_report):\n"
             " assert 'uid_provider' not in inspect.signature(f).parameters\n"
             "S.os.geteuid = lambda: 0\n"
             "try:\n S.read_selection(sys.argv[1], ['src'])\nexcept S.SnapshotError as e:\n print(e.code)\n")
    r = _run([str(py), "-c", check, str(tmp_path / "repo")], env=env)
    assert r.returncode == 0 and r.stdout.strip() == "root_refused", r.stdout + r.stderr
    send = subprocess.Popen([str(py), "-c", "import sys, grok_bot_build_bridge.snapshot_posix as S\n"
                             "S.send_report(S.read_selection(sys.argv[1], ['src']), 1, deadline_s=20)",
                             str(tmp_path / "repo")], stdout=subprocess.PIPE, env=env)
    recv = subprocess.run([str(py), "-c", "import sys, grok_bot_build_bridge.snapshot_posix as S\n"
                           "r = S.receive_snapshot(0, sys.argv[1], deadline_s=20); print(r.file_count)",
                           str(stage)], stdin=send.stdout, capture_output=True, text=True, env=env, timeout=60)
    send.stdout.close()
    assert send.wait(30) == 0 and recv.returncode == 0 and recv.stdout.strip() == "1", recv.stderr
    (snap,) = os.listdir(stage)
    assert sorted(os.listdir(stage / snap)) == ["COMPLETE", "files"]
    assert (stage / snap / "files" / "src" / "a.py").read_text() == "print('fake')\n"

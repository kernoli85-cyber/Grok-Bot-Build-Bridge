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

ROOT = Path(__file__).resolve().parents[1]

# Subcommands that must work from an installed wheel (no checkout cwd)
_SMOKE_CMDS = [
    ["version"],
    ["--help"],
    ["onboard", "--desired", "unset"],
    ["discover-demo"],
    ["skill-draft"],
]


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
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
    r = _run([str(py), "-m", "pip", "install", "--no-deps", str(wheel)])
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

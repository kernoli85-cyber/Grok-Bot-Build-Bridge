"""End-to-end dummy workflow from a freshly built/installed wheel.

No paid Grok, no real external webhook sends — mocked HTTPS receiver only.
Clarifies: real runner uses ApprovedExecutionPlan + dummy/bridge-dummy or discovered
grok.exe/.ps1; cancel remains state-only; outbox delivery is separate from job reexec.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(
    cmd: list[str], *, cwd: Path | None = None, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture(scope="module")
def installed_wheel_env(tmp_path_factory):
    build_dir = tmp_path_factory.mktemp("build")
    dist = build_dir / "dist"
    r = _run([sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)], cwd=ROOT)
    if r.returncode != 0:
        r2 = _run(
            [sys.executable, "-m", "pip", "wheel", ".", "-w", str(dist), "--no-deps"],
            cwd=ROOT,
        )
        assert r2.returncode == 0, r.stdout + r.stderr + r2.stdout + r2.stderr
    wheels = list(dist.glob("grok_bot_build_bridge-*.whl"))
    assert wheels, f"no wheel in {list(dist.iterdir())}"
    wheel = wheels[0]

    venv_dir = tmp_path_factory.mktemp("venv")
    venv.create(venv_dir, with_pip=True)
    if sys.platform.startswith("win"):
        py = venv_dir / "Scripts" / "python.exe"
        exe = venv_dir / "Scripts" / "grok-bot-build-bridge.exe"
    else:
        py = venv_dir / "bin" / "python"
        exe = venv_dir / "bin" / "grok-bot-build-bridge"
    env0 = os.environ.copy()
    env0.pop("PYTHONPATH", None)
    env0["PYTHONNOUSERSITE"] = "1"
    inst = _run(
        [str(py), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)],
        env=env0,
    )
    assert inst.returncode == 0, inst.stdout + inst.stderr
    assert exe.exists(), f"missing entrypoint {exe}; pip={inst.stdout}\n{inst.stderr}"

    cfg = tmp_path_factory.mktemp("cfg")
    data = tmp_path_factory.mktemp("data")
    work = tmp_path_factory.mktemp("work")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if sys.platform.startswith("win"):
        env["LOCALAPPDATA"] = str(cfg)
    else:
        env["XDG_CONFIG_HOME"] = str(cfg)
        env["XDG_DATA_HOME"] = str(data)
    return {"exe": exe, "py": py, "work": work, "env": env, "wheel": wheel}


def test_e2e_onboard_view_job_dummy_outbox_mocked(installed_wheel_env, tmp_path: Path):
    exe = installed_wheel_env["exe"]
    py = installed_wheel_env["py"]
    work = installed_wheel_env["work"]
    env = installed_wheel_env["env"]

    # 1) onboard dry-run from installed wheel entrypoint
    onboard = _run([str(exe), "onboard", "--desired", "unset"], cwd=work, env=env)
    assert onboard.returncode == 0, onboard.stdout + onboard.stderr
    plan = json.loads(onboard.stdout)
    assert plan["schema_version"] == 1
    assert plan["pairing"]["auth_ready_claim"] is False

    plan_path = work / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    view = _run(
        [str(exe), "onboard-view", "--plan-json", str(plan_path)],
        cwd=work,
        env=env,
    )
    assert view.returncode == 0, view.stdout + view.stderr
    view_obj = json.loads(view.stdout)
    assert view_obj["pairing"]["auth_ready_claim"] is False
    for item in view_obj.get("checklist", []):
        assert item.get("treat_as_shell") is False

    # 2) job + dummy execute + mocked outbox receiver (installed package import)
    dummy_src = ROOT / "tests" / "fixtures" / "bridge-dummy.py"
    assert dummy_src.is_file()
    dummy = work / "bridge-dummy-e2e.py"
    dummy.write_text(dummy_src.read_text(encoding="utf-8"), encoding="utf-8")
    dummy.chmod(0o755)
    db = work / "e2e.sqlite"

    flow = work / "e2e_flow.py"
    flow.write_text(
        f"""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import MagicMock

from grok_bot_build_bridge.state.clock import Clock, Jitter
from grok_bot_build_bridge.state.store import JobStore
from grok_bot_build_bridge.state.types import JobStatus
from grok_bot_build_bridge.runner.plan import build_approved_plan
from grok_bot_build_bridge.runner.worker import execute_one_shot
from grok_bot_build_bridge.transport.webhook import WebhookConfig, WebhookTransport
from grok_bot_build_bridge.transport.pump import pump_outbox

work = Path({str(work)!r})
db = Path({str(db)!r})
dummy = Path({str(dummy)!r})

clock = Clock()
clock.set(9000.0)
store = JobStore(db, clock=clock, jitter=Jitter(seed=3))

plan = build_approved_plan(
    task_id="e2e-1",
    backend="native",
    cwd=str(work),
    executable=str(dummy),
    prompt="",
    prompt_via_stdin=True,
    timeout_sec=15.0,
)
out = execute_one_shot(store, plan, owner="e2e-worker", dry_run=False, lease_sec=120.0)
assert out.status == JobStatus.SUCCEEDED.value, (out.status, out.error_code, out.note)
job = store.get_job("e2e-1")
assert job is not None and job.status == JobStatus.SUCCEEDED

events = store.list_outbox()
assert events, "lifecycle must enqueue outbox events"

opener = MagicMock()
class _Resp:
    status = 204
    def read(self, n=-1):
        return b""
    def getcode(self):
        return 204
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
opener.open.return_value = _Resp()
cfg = WebhookConfig(url="https://hooks.example.com/e2e", bearer_token="e2e-mock-token")
transport = WebhookTransport(cfg, opener=opener)
results = pump_outbox(store, transport, dry_run=False, limit=50)
assert opener.open.called, results
req = opener.open.call_args[0][0]
assert req.full_url.startswith("https://hooks.example.com")
assert "e2e-mock-token" in (req.get_header("Authorization") or "")
# job status unchanged by outbox delivery
assert store.get_job("e2e-1").status == JobStatus.SUCCEEDED
print(json.dumps({{"ok": True, "pumped": len(results), "outbox": len(events)}}))
""",
        encoding="utf-8",
    )
    ran = _run([str(py), str(flow)], cwd=work, env=env)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert '"ok": true' in ran.stdout.lower() or '"ok": true' in ran.stdout or json.loads(ran.stdout.strip().splitlines()[-1])["ok"] is True


def test_runner_capability_honesty_in_docs():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    # Must not claim production process-tree cancel
    assert "state-only" in readme.lower() or "STATE-ONLY" in readme or "state only" in readme.lower()
    dist = (ROOT / "docs" / "DISTRIBUTION.md").read_text(encoding="utf-8")
    assert "unverified" in dist.lower()

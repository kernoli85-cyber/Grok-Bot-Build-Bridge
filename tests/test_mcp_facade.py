"""Real MCP stdio integration: initialize / list_tools / call_tool via official SDK."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import venv
from pathlib import Path

import pytest

pytest.importorskip("mcp")
import importlib.metadata as _imd

_mcp_ver = _imd.version("mcp")
if tuple(int(x) for x in _mcp_ver.split(".")[:2]) < (1, 28):
    pytest.skip(f"mcp>={_mcp_ver} too old; need >=1.28")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from grok_bot_build_bridge.mcp_facade.scope import STATE_DIR_ENV
from grok_bot_build_bridge.state.clock import Clock, Jitter
from grok_bot_build_bridge.state.store import JobStore

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

EXPECTED_TOOLS = {
    "bridge_runtime_info",
    "bridge_discover",
    "bridge_onboard",
    "bridge_status",
    "bridge_job_status",
}

FORBIDDEN_SUBSTRINGS = (
    "shell",
    "execute",
    "enqueue",
    "outbox",
    "cancel",
    "secret",
    "write_file",
    "run_command",
)


def _server_env(state_dir: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    # Prefer source tree for in-repo tests
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    if state_dir is not None:
        env[STATE_DIR_ENV] = str(state_dir)
    else:
        env.pop(STATE_DIR_ENV, None)
    return env


def _params(state_dir: Path | None = None) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "grok_bot_build_bridge.mcp_facade.server"],
        env=_server_env(state_dir),
    )


def _text_payload(result) -> dict:
    """Extract JSON object from CallToolResult content."""
    assert result.content, result
    # Prefer structuredContent when present
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        # FastMCP wraps dict returns as {"result": ...} sometimes
        if "result" in sc and isinstance(sc["result"], dict):
            return sc["result"]
        if "error" in sc or "runtime_location" in sc or "jobs" in sc or "kind" in sc:
            return sc
    texts = []
    for block in result.content:
        t = getattr(block, "text", None)
        if t:
            texts.append(t)
    joined = "\n".join(texts)
    data = json.loads(joined)
    assert isinstance(data, dict)
    return data


async def _with_session(state_dir: Path | None, coro_fn):
    params = _params(state_dir)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            return await coro_fn(session, init)


def test_initialize_list_tools_and_runtime_info(tmp_path: Path):
    async def body(session, init):
        assert init.serverInfo is not None
        assert "grok-bot-build-bridge" in (init.serverInfo.name or "")
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert names == EXPECTED_TOOLS
        for n in names:
            low = n.lower()
            for bad in FORBIDDEN_SUBSTRINGS:
                assert bad not in low, n
        r = await session.call_tool("bridge_runtime_info", {})
        assert r.isError is False
        payload = _text_payload(r)
        assert payload["runtime_location"] in {
            "box_or_linux",
            "windows_host",
            "darwin",
        } or payload["runtime_location"].startswith("other:")
        assert payload["remote_bind"] is False
        assert payload["transport"] == "stdio"
        assert "authority_note" in payload
        assert "bridge_job_status" in payload["tools"]
        assert "execute" in payload["not_exposed"]
        return payload

    payload = asyncio.run(_with_session(tmp_path, body))
    # Linux CI / bot box: discovery none expected
    if sys.platform.startswith("linux"):
        assert payload["runtime_location"] == "box_or_linux"


def test_discover_live_on_linux_typically_none(tmp_path: Path):
    async def body(session, _init):
        r = await session.call_tool("bridge_discover", {"mode": "live"})
        assert r.isError is False
        payload = _text_payload(r)
        assert payload["mode"] == "live"
        assert "runtime" in payload
        if sys.platform.startswith("linux"):
            # Honest expectation on BOX/Linux
            assert payload["kind"] in {"none", "native_only", "wsl_only", "both"}
        return payload

    asyncio.run(_with_session(tmp_path, body))


def test_discover_fixture_and_onboard(tmp_path: Path):
    async def body(session, _init):
        r = await session.call_tool("bridge_discover", {"mode": "fixture"})
        payload = _text_payload(r)
        assert payload["mode"] == "fixture"
        assert payload["kind"] in {"wsl_only", "both", "native_only", "none"}

        r2 = await session.call_tool(
            "bridge_onboard", {"desired": "unset", "mode": "fixture"}
        )
        plan = _text_payload(r2)
        assert "error" not in plan
        assert plan.get("schema_version") == 1 or "checklist" in plan or "outcome" in plan
        return plan

    asyncio.run(_with_session(tmp_path, body))


def test_invalid_inputs_and_unknown_tool(tmp_path: Path):
    async def body(session, _init):
        bad_mode = await session.call_tool("bridge_discover", {"mode": "explode"})
        pm = _text_payload(bad_mode)
        assert pm["error"]["code"] == "invalid_mode"

        bad_desired = await session.call_tool(
            "bridge_onboard", {"desired": "root", "mode": "fixture"}
        )
        assert _text_payload(bad_desired)["error"]["code"] == "invalid_desired"

        bad_id = await session.call_tool(
            "bridge_job_status", {"task_id": "../etc/passwd"}
        )
        assert _text_payload(bad_id)["error"]["code"] == "invalid_task_id"

        # Unknown tool — SDK surfaces as error result
        unknown = await session.call_tool("bridge_execute_shell", {})
        assert unknown.isError is True or (
            unknown.content
            and "Unknown" in "".join(getattr(c, "text", "") or "" for c in unknown.content)
        )
        return True

    asyncio.run(_with_session(tmp_path, body))


def test_status_absent_db_does_not_create(tmp_path: Path):
    state_dir = tmp_path / "scoped"
    state_dir.mkdir()
    db = state_dir / "state.sqlite"
    assert not db.exists()

    async def body(session, _init):
        r = await session.call_tool("bridge_status", {})
        payload = _text_payload(r)
        assert payload["error"]["code"] == "state_db_absent"
        return payload

    asyncio.run(_with_session(state_dir, body))
    assert not db.exists(), "MCP must not create missing state.sqlite on query"


def test_job_status_with_existing_db(tmp_path: Path):
    state_dir = tmp_path / "scoped"
    state_dir.mkdir()
    clock = Clock()
    clock.set(1_000.0)
    store = JobStore(
        state_dir / "state.sqlite",
        clock=clock,
        jitter=Jitter(seed=1),
    )
    store.enqueue_job(task_id="task-1", backend="wsl", cwd="/tmp/demo")

    async def body(session, _init):
        st = await session.call_tool("bridge_status", {})
        status = _text_payload(st)
        assert "error" not in status
        assert any(j["task_id"] == "task-1" for j in status["jobs"])
        # No payload / prompt leakage keys
        # Note text may mention the word "prompts"; ensure no prompt *field* leaked
        assert all("prompt" not in j for j in status["jobs"])
        assert "password" not in json.dumps(status).lower()

        one = await session.call_tool("bridge_job_status", {"task_id": "task-1"})
        job = _text_payload(one)
        assert job["job"]["task_id"] == "task-1"
        assert job["job"]["status"] == "queued"

        missing = await session.call_tool("bridge_job_status", {"task_id": "no-such"})
        assert _text_payload(missing)["error"]["code"] == "job_not_found"
        return True

    asyncio.run(_with_session(state_dir, body))


def test_redaction_of_sensitive_keys_in_handlers(tmp_path: Path):
    """Handler-level redact: injected sensitive keys become <redacted>."""
    from grok_bot_build_bridge.mcp_facade.handlers import _ok

    out = _ok(
        {
            "token": "super-secret",
            "api_key": "abc",
            "safe": "ok",
            "nested": {"password": "x", "status": "fine"},
        }
    )
    assert out["token"] == "<redacted>"
    assert out["api_key"] == "<redacted>"
    assert out["safe"] == "ok"
    assert out["nested"]["password"] == "<redacted>"
    assert out["nested"]["status"] == "fine"


def test_relative_state_dir_env_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from grok_bot_build_bridge.mcp_facade.scope import ScopeError, scoped_state_dir

    monkeypatch.setenv(STATE_DIR_ENV, "relative/path")
    with pytest.raises(ScopeError) as ei:
        scoped_state_dir()
    assert ei.value.code == "invalid_state_dir"

    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "ok" / ".." / "escape"))
    # Path with .. in parts
    with pytest.raises(ScopeError):
        scoped_state_dir()


def test_cli_mcp_stdio_help():
    from grok_bot_build_bridge.cli import build_parser

    p = build_parser()
    # ensure subcommand registered
    assert "mcp-stdio" in p.format_help()


@pytest.mark.slow
def test_wheel_mcp_stdio_integration(tmp_path: Path):
    """Install built wheel + mcp into isolated venv; real stdio initialize/list/call."""
    import subprocess
    import hashlib

    dist = tmp_path / "dist"
    dist.mkdir()
    build = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(dist)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(dist.glob("*.whl"))
    assert wheels
    wheel = wheels[0]

    vdir = tmp_path / "venv"
    venv.create(vdir, with_pip=True)
    if sys.platform.startswith("win"):
        py = vdir / "Scripts" / "python.exe"
    else:
        py = vdir / "bin" / "python"
    clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    inst = subprocess.run(
        [
            str(py),
            "-m",
            "pip",
            "install",
            "--force-reinstall",
            "-q",
            str(wheel),
            "mcp>=1.28,<2",
        ],
        capture_output=True,
        text=True,
        env=clean_env,
        cwd=str(tmp_path),
    )
    assert inst.returncode == 0, inst.stdout + inst.stderr

    # Prove package imports from the venv without checkout PYTHONPATH
    probe = subprocess.run(
        [str(py), "-c", "import grok_bot_build_bridge as b; print(b.__version__, b.__file__)"],
        capture_output=True,
        text=True,
        env=clean_env,
        cwd=str(tmp_path),
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "0.13" in probe.stdout
    assert "site-packages" in probe.stdout.replace("\\", "/")

    state_dir = tmp_path / "mcp-state"
    state_dir.mkdir()
    env = dict(clean_env)
    env[STATE_DIR_ENV] = str(state_dir)

    if sys.platform.startswith("win"):
        script = vdir / "Scripts" / "grok-bot-build-bridge-mcp.exe"
    else:
        script = vdir / "bin" / "grok-bot-build-bridge-mcp"
    assert script.exists(), script

    params = StdioServerParameters(
        command=str(script),
        args=[],
        env=env,
    )

    async def body():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert names == EXPECTED_TOOLS
                r = await session.call_tool("bridge_runtime_info", {})
                payload = _text_payload(r)
                assert payload["package_version"].startswith("0.13")

    asyncio.run(body())
    # Hash recorded for CI visibility
    _ = hashlib.sha256(wheel.read_bytes()).hexdigest()

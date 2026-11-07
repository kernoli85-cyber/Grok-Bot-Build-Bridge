"""Read-only status (CLI + MCP), opt-in MCP result metadata tool, no DB mutation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from grok_bot_build_bridge import cli
from grok_bot_build_bridge.runner import build_approved_plan, execute_one_shot
from grok_bot_build_bridge.safe_exec import RunResult
from grok_bot_build_bridge.state import JobStore
from grok_bot_build_bridge.state.readonly import ReadOnlyState, ReadOnlyStateError

FIX = Path(__file__).parent / "fixtures"
SRC = Path(__file__).resolve().parents[1] / "src"
OUT = ('{"text":"secret reply TEXT","stopReason":"end_turn","sessionId":"sess-PRIVATE-9",'
       '"num_turns":1,"usage":{"input_tokens":3},"total_cost_usd":0.30000000000000004}')


def _make_db(d: Path, n_plain: int = 2) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    db = d / "state.sqlite"
    st = JobStore(db)
    p = build_approved_plan(task_id="res-1", backend="wsl", cwd="/tmp",
                            executable=str(FIX / "bridge-dummy-grokjson.py"), extra_args=(), prompt=None)
    p = replace(p, parse_grok_json=True)
    execute_one_shot(st, p, owner="w", dry_run=False,
                     executor=lambda a, **k: RunResult(tuple(a), 0, OUT, ""))
    for i in range(n_plain):
        st.enqueue_job(task_id=f"plain-{i}", backend="wsl", cwd="/home/okern/private-proj")
    del st, p
    import gc
    gc.collect()  # close lingering writer connections -> WAL checkpointed, sidecars gone
    return db


def _snapshot(d: Path):
    """Main DB file only. WAL sidecars (-shm/-wal) may legitimately appear for ro readers."""
    p = d / "state.sqlite"
    return (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns, p.stat().st_mode)


# ---------- ReadOnlyState ----------

def test_ro_absent_never_created(tmp_path):
    with pytest.raises(ReadOnlyStateError) as e:
        ReadOnlyState(tmp_path / "nope.sqlite").open()
    assert e.value.code == "state_db_absent" and not (tmp_path / "nope.sqlite").exists()
    assert str(tmp_path) not in e.value.message


def test_ro_no_mutation_no_sidecars(tmp_path):
    db = _make_db(tmp_path / "s")
    before = _snapshot(tmp_path / "s")
    with ReadOnlyState(db) as ro:
        assert ro.info.access_mode == "ro_snapshot"
        assert ro.get_job("res-1")["status"] == "succeeded"
        assert ro.get_result("res-1")["session_id"] == "sess-PRIVATE-9"
    assert _snapshot(tmp_path / "s") == before


def test_ro_with_live_writer_uses_ro_mode(tmp_path):
    db = _make_db(tmp_path / "s")
    w = sqlite3.connect(db)
    w.execute("UPDATE meta SET value=value")
    w.commit()
    try:
        if os.path.exists(str(db) + "-wal"):
            with ReadOnlyState(db) as ro:
                assert ro.info.access_mode == "ro_snapshot"
                assert ro.get_job("res-1") is not None
    finally:
        w.close()


@pytest.mark.parametrize("content,code", [(b"", "state_db_invalid"), (b"x" * 200, "state_db_invalid")])
def test_ro_garbage(tmp_path, content, code):
    p = tmp_path / "state.sqlite"
    p.write_bytes(content)
    with pytest.raises(ReadOnlyStateError) as e:
        ReadOnlyState(p).open()
    assert e.value.code == code


def test_ro_foreign_schema_and_old_schema(tmp_path):
    p = tmp_path / "foreign.sqlite"
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE other(x)")
    with pytest.raises(ReadOnlyStateError) as e:
        ReadOnlyState(p).open()
    assert e.value.code == "state_db_schema_unsupported"
    db = _make_db(tmp_path / "old")
    with sqlite3.connect(db) as c:
        c.execute("DROP TABLE job_results_v2")
    with ReadOnlyState(db) as ro:  # pre-0.16.4 DB: readable, no results, not migrated
        assert ro.get_result("res-1") is None
    with sqlite3.connect(db) as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='job_results_v2'").fetchall()


def test_ro_tampered_result_row_ignored(tmp_path):
    db = _make_db(tmp_path / "s")
    with sqlite3.connect(db) as c:
        c.execute("UPDATE job_results_v2 SET summary_json='{\"matched_text\":\"pwn\"}'")
    with ReadOnlyState(db) as ro:
        assert ro.get_result("res-1") is None


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink perms on Windows")
def test_ro_symlink_rejected(tmp_path):
    db = _make_db(tmp_path / "s")
    link = tmp_path / "link.sqlite"
    link.symlink_to(db)
    with pytest.raises(ReadOnlyStateError) as e:
        ReadOnlyState(link).open()
    assert e.value.code == "state_db_not_regular"


# ---------- CLI jobs-status ----------

def test_jobs_status_task_scoped_and_pagination(tmp_path, capsys):
    db = _make_db(tmp_path / "s", n_plain=5)
    before = _snapshot(tmp_path / "s")
    assert cli.main(["jobs-status", "--state-db", str(db), "--task-id", "plain-3"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert [j["task_id"] for j in d["jobs"]] == ["plain-3"] and d["task_id_filter"] == "plain-3"
    assert all(e["task_id"] == "plain-3" for e in d["outbox"])
    assert cli.main(["jobs-status", "--state-db", str(db), "--limit", "2", "--offset", "1"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert len(d["jobs"]) == 2 and d["total_jobs"] == 6 and d["read_only"] is True
    assert cli.main(["jobs-status", "--state-db", str(db), "--limit", "99999"]) == 0
    assert json.loads(capsys.readouterr().out)["limit"] == 500
    assert _snapshot(tmp_path / "s") == before


def test_jobs_status_unknown_and_absent(tmp_path, capsys):
    db = _make_db(tmp_path / "s")
    assert cli.main(["jobs-status", "--state-db", str(db), "--task-id", "nope"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "job_not_found"
    assert cli.main(["jobs-status", "--state-db", str(db), "--task-id", "../x"]) == 2
    capsys.readouterr()
    missing = tmp_path / "m" / "none.db"
    assert cli.main(["jobs-status", "--state-db", str(missing)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "state_db_absent"
    assert not missing.exists() and not missing.parent.exists()


def test_jobs_status_text_format(tmp_path, capsys):
    db = _make_db(tmp_path / "s")
    assert cli.main(["jobs-status", "--state-db", str(db), "--task-id", "res-1", "--show-result",
                     "--format", "text"]) == 0
    out = capsys.readouterr().out
    assert "Job res-1 is succeeded" in out and "Cost reported by Grok: US$ 0.30000000000000004." in out
    assert "sess-PRIVATE-9" not in out and "secret reply" not in out
    assert cli.main(["jobs-status", "--state-db", str(db), "--format", "text"]) == 0
    assert "Showing 3 of 3 jobs" in capsys.readouterr().out


# ---------- MCP handlers + protocol ----------

def _payload(result) -> dict:
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc["result"] if isinstance(sc.get("result"), dict) else sc
    return json.loads("\n".join(b.text for b in result.content if getattr(b, "text", None)))


def _stdio(state_dir: Path, env_extra: dict, fn):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["GROK_BOT_BUILD_BRIDGE_MCP_STATE_DIR"] = str(state_dir)
    for k in ("GROK_BOT_BUILD_BRIDGE_MCP_RESULTS", "GROK_BOT_BUILD_BRIDGE_MCP_RESULTS_COST"):
        env.pop(k, None)
    env.update(env_extra)
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "grok_bot_build_bridge.mcp_facade.server"], env=env)

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)
    return asyncio.run(go())


def test_mcp_stdio_result_tool_disabled_by_default(tmp_path):
    db = _make_db(tmp_path / "s")
    before = _snapshot(tmp_path / "s")

    async def fn(s):
        names = {t.name for t in (await s.list_tools()).tools}
        assert "bridge_job_result" not in names
        r = await s.call_tool("bridge_job_result", {"task_id": "res-1"})
        assert r.isError
        js = _payload(await s.call_tool("bridge_job_status", {"task_id": "plain-0"}))
        blob = json.dumps(js)
        assert js["job"]["status"] == "queued"
        assert "/home/okern" not in blob and str(tmp_path) not in blob and "lease_owner" not in blob
        st = _payload(await s.call_tool("bridge_status", {}))
        assert st["total_jobs"] == 3 and str(tmp_path) not in json.dumps(st)
    _stdio(tmp_path / "s", {}, fn)
    assert _snapshot(tmp_path / "s") == before


@pytest.mark.parametrize("cost", [False, True])
def test_mcp_stdio_result_tool_enabled_metadata_only(tmp_path, cost):
    db = _make_db(tmp_path / "s")
    before = _snapshot(tmp_path / "s")
    extra = {"GROK_BOT_BUILD_BRIDGE_MCP_RESULTS": "1"}
    if cost:
        extra["GROK_BOT_BUILD_BRIDGE_MCP_RESULTS_COST"] = "1"

    async def fn(s):
        assert "bridge_job_result" in {t.name for t in (await s.list_tools()).tools}
        d = _payload(await s.call_tool("bridge_job_result", {"task_id": "res-1"}))
        blob = json.dumps(d)
        for bad in ("sess-PRIVATE-9", "secret reply", "text_sha256", "text_len", "session_id",
                    "matched_text", str(tmp_path)):
            assert bad not in blob
        assert d["result_metadata"]["parse_status"] == "ok" and d["result_metadata"]["stop_reason"] == "end_turn"
        if cost:
            assert d["result_metadata"]["total_cost_usd"] == "0.30000000000000004"
        else:
            assert "total_cost_usd" not in d["result_metadata"] and d["cost_included"] is False
        bad = _payload(await s.call_tool("bridge_job_result", {"task_id": "x;rm"}))
        assert bad["error"]["code"] == "invalid_task_id"
        nf = _payload(await s.call_tool("bridge_job_result", {"task_id": "ghost"}))
        assert nf["error"]["code"] == "job_not_found"
        # caller cannot toggle cost via arguments
        r = await s.call_tool("bridge_job_result", {"task_id": "res-1", "include_cost": True})
        if not r.isError:
            assert ("total_cost_usd" in json.dumps(_payload(r))) == cost
    _stdio(tmp_path / "s", extra, fn)
    assert _snapshot(tmp_path / "s") == before


def test_mcp_stdio_absent_db_not_created(tmp_path):
    async def fn(s):
        d = _payload(await s.call_tool("bridge_status", {}))
        assert d["error"]["code"] == "state_db_absent"
    _stdio(tmp_path / "empty", {"GROK_BOT_BUILD_BRIDGE_MCP_RESULTS": "1"}, fn)
    assert not (tmp_path / "empty").exists()


def _asgi_env(monkeypatch, state_dir, **extra):
    from grok_bot_build_bridge.mcp_facade import http_config as hc
    monkeypatch.setenv(hc.BEARER_ENV, "test-static-bearer-NOT-A-SECRET-0002")
    monkeypatch.setenv(hc.RESOURCE_URL_ENV, "http://127.0.0.1/mcp")
    monkeypatch.setenv(hc.ISSUER_URL_ENV, "http://127.0.0.1/")
    monkeypatch.setenv(hc.ALLOWED_HOSTS_ENV, "127.0.0.1")
    monkeypatch.setenv(hc.ALLOWED_ORIGINS_ENV, "http://127.0.0.1")
    monkeypatch.setenv(hc.ALLOW_INSECURE_LOOPBACK_ENV, "1")
    monkeypatch.setenv("GROK_BOT_BUILD_BRIDGE_MCP_STATE_DIR", str(state_dir))
    monkeypatch.delenv("GROK_BOT_BUILD_BRIDGE_MCP_RESULTS", raising=False)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize("enabled", [False, True])
def test_mcp_asgi_result_tool(tmp_path, monkeypatch, enabled):
    pytest.importorskip("mcp")
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from grok_bot_build_bridge.mcp_facade.http_app import create_streamable_http_app
    _make_db(tmp_path / "s")
    before = _snapshot(tmp_path / "s")
    _asgi_env(monkeypatch, tmp_path / "s",
              **({"GROK_BOT_BUILD_BRIDGE_MCP_RESULTS": "1"} if enabled else {}))
    app = create_streamable_http_app()

    async def body():
        star = app.app
        async with star.router.lifespan_context(star):
            tr = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=tr, base_url="http://127.0.0.1") as bare:
                r = await bare.post("/mcp", headers={"Host": "127.0.0.1",
                                    "Accept": "application/json, text/event-stream",
                                    "Content-Type": "application/json"},
                                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": "bridge_job_result", "arguments": {"task_id": "res-1"}}})
                assert r.status_code == 401
            hdr = {"Authorization": "Bearer test-static-bearer-NOT-A-SECRET-0002", "Host": "127.0.0.1"}
            async with httpx.AsyncClient(transport=tr, base_url="http://127.0.0.1", headers=hdr) as hc:
                async with streamable_http_client("http://127.0.0.1/mcp", http_client=hc) as (rd, wr, _):
                    async with ClientSession(rd, wr) as s:
                        await s.initialize()
                        names = {t.name for t in (await s.list_tools()).tools}
                        assert ("bridge_job_result" in names) == enabled
                        res = await s.call_tool("bridge_job_result", {"task_id": "res-1"})
                        if enabled:
                            d = _payload(res)
                            assert d["result_metadata"]["model_ok"] is True and "sess-PRIVATE-9" not in json.dumps(d)
                        else:
                            assert res.isError
    asyncio.run(body())
    assert _snapshot(tmp_path / "s") == before


def test_static_snapshot_only_when_operator_asserts(tmp_path):
    db = _make_db(tmp_path / "s")
    names = sorted(x.name for x in (tmp_path / "s").iterdir())
    with ReadOnlyState(db, assume_static_snapshot=True) as ro:
        assert ro.info.access_mode == "immutable_operator_asserted"
        assert ro.get_job("res-1") is not None
    assert sorted(x.name for x in (tmp_path / "s").iterdir()) == names  # no sidecars


def test_reader_snapshot_coherent_under_concurrent_writer(tmp_path):
    """A writer finishing the job between reader queries must not produce a mixed view."""
    from grok_bot_build_bridge.state import JobStatus
    from grok_bot_build_bridge.runner.grok_result import parse_grok_json_result
    db = tmp_path / "c" / "state.sqlite"
    db.parent.mkdir()
    st = JobStore(db)
    st.enqueue_job(task_id="c1", backend="wsl", cwd="/tmp")
    st.claim_job("c1", owner="w", lease_sec=300)
    st.mark_running("c1", owner="w")
    with ReadOnlyState(db) as ro:
        assert ro.get_job("c1")["status"] == "running"  # snapshot pinned here
        summary = parse_grok_json_result(OUT).to_dict()
        st.commit_job_result("c1", owner="w", summary=summary, terminal=JobStatus.SUCCEEDED)
        assert ro.get_job("c1")["status"] == "running"
        assert ro.get_result("c1") is None
        assert all(e["event_type"] != "job.succeeded" for e in ro.outbox_for("c1"))
    with ReadOnlyState(db) as ro:
        assert ro.get_job("c1")["status"] == "succeeded" and ro.get_result("c1") is not None

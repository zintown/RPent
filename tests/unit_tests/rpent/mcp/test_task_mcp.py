# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

"""Exercise the public Streamable HTTP MCP task contract."""

import asyncio
import json
import shlex
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from anyio import fail_after
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from rpent.mcp.task.server import create_app

TOKEN = "offline-test-token"
URL = "http://127.0.0.1:8780"
CHILD = r"""
import json, os, signal, sys, time
from pathlib import Path

root = Path.cwd()
output = Path(os.environ["RPENT_DISPATCH_OUTPUT_DIR"])
instruction = sys.argv[1]
with (root / 'launches.jsonl').open('a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
if instruction == 'wait':
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    (root / 'ready').touch()
    while True:
        time.sleep(.05)
finish = {'status': 'failure' if instruction == 'failure' else 'success', 'summary': 'checked'}
(output / 'transcript_robot_test.json').write_text(json.dumps({'finish': finish, 'error': None}))
"""

ARM_SERVICE = """
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("arm")
output_dir = Path(os.environ["RPENT_TASK_OUTPUT_DIR"])
assert output_dir.is_dir()

@server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def get_status() -> dict:
    return {"ready": Path("arm-ready").exists()}

@server.tool()
def grasp() -> dict:
    Path("grasped").touch()
    return {"ready": True}

server.run()
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "child.py").write_text(CHILD)
    (tmp_path / "run_rpent.sh").write_text(
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(tmp_path / "child.py"))} "$@"\n'
    )
    (tmp_path / "mcp.toml").write_text(
        '[robot]\nid = "go2-test"\ntype = "unitree.go2"\nenvironment = "simulation"\n'
        'adapter = "unitree"\n[mcp_servers]\n'
    )
    return tmp_path


def make_app(root: Path, **kwargs):
    return create_app(
        repo_root=root,
        jobs_dir=root / "jobs",
        token=TOKEN,
        mcp_config=root / "mcp.toml",
        **kwargs,
    )


@asynccontextmanager
async def connect(app, path="/mcp"):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"Authorization": "Bearer " + TOKEN},
            base_url=URL,
        ) as http:
            async with streamable_http_client(URL + path, http_client=http) as (
                read,
                write,
                session_id,
            ):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    yield session, http, initialized, session_id


async def run_task(session, instruction):
    result = await session.call_tool("run_task", {"instruction": instruction})
    assert not result.isError, result
    return result.structuredContent


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_run_task_schema_and_terminal_result(root, path):
    async def run():
        async with connect(make_app(root), path) as (session, http, info, session_id):
            tools = (await session.list_tools()).tools
            assert [tool.name for tool in tools] == ["get_robot_info", "run_task"]
            assert tools[0].annotations.readOnlyHint
            assert not tools[0].inputSchema["properties"]
            assert set(tools[1].inputSchema["properties"]) == {"instruction"}
            assert "run_task" in info.instructions
            assert session_id()
            instruction = '查询 Go2 位姿 "quoted"'
            result = await run_task(session, instruction)
            assert result == {
                "robot_id": "go2-test",
                "status": "succeeded",
                "summary": "checked",
                "error": None,
                "output_dir": result["output_dir"],
                "evidence": {},
                "navigation": None,
            }
            assert Path(result["output_dir"]).is_dir()
            assert (await http.post("/api/rpend/run", json={})).status_code == 404
            assert not (await http.get("/healthz")).json()["busy"]
            repeated = await run_task(session, instruction)
            assert repeated["output_dir"] != result["output_dir"]
        launches = [
            json.loads(line)
            for line in (root / "launches.jsonl").read_text().splitlines()
        ]
        assert launches == [[instruction], [instruction]]

    asyncio.run(run())


def test_capabilities_reflect_discovered_tools_and_live_read_only_checks(root):
    (root / "arm.py").write_text(ARM_SERVICE)
    config = root / "mcp.toml"
    config.write_text(
        '[robot]\nid = "arm-02"\ntype = "franka.panda"\nenvironment = "real"\n'
        '[mcp_servers.arm]\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["arm.py"]\ncwd = "."\nenv_from = ["RPENT_TASK_OUTPUT_DIR"]\n'
        '[mcp_servers.localization]\nurl = "http://127.0.0.1:1/mcp"\nenabled = false\n'
        '[capabilities.grasp]\ndescription = "Pick up an object"\ntools = ["arm.grasp"]\n'
        'checks = { "arm.get_status" = { ready = true } }\n'
        '[capabilities.map_navigation]\ndescription = "Navigate a map"\n'
        'tools = ["localization.get_pose", "arm.grasp"]\n'
        '[capabilities.inspection]\ndescription = "Read status"\n'
        'tools = ["arm.get_status"]\n'
        '[capabilities.bad_probe]\ndescription = "Misconfigured check"\n'
        'tools = ["arm.grasp"]\nchecks = { "arm.grasp" = { ready = true } }\n'
    )

    async def run():
        async with connect(make_app(root)) as (session, *_):
            result = await session.call_tool("get_robot_info", {})
            assert not result.isError, result
            info = result.structuredContent
            assert info["robot_id"] == "arm-02"
            assert info["robot_type"] == "franka.panda"
            assert info["environment"] == "real"
            assert info["state"] == "idle"
            assert info["service_errors"] == {}
            assert {cap["name"]: cap["status"] for cap in info["capabilities"]} == {
                "grasp": "unavailable",
                "map_navigation": "unavailable",
                "inspection": "unverified",
                "bad_probe": "unavailable",
            }
            (root / "arm-ready").touch()
            info = (await session.call_tool("get_robot_info", {})).structuredContent
            assert (
                next(cap for cap in info["capabilities"] if cap["name"] == "grasp")[
                    "status"
                ]
                == "ready"
            )
            assert not (root / "grasped").exists()
            assert not (root / "launches.jsonl").exists()
            assert (await run_task(session, "测试机械臂任务"))["robot_id"] == "arm-02"

    asyncio.run(run())


def test_mcp_cancellation_stops_only_owned_request_and_releases_runner(root):
    async def run():
        async with connect(make_app(root)) as (session, http, _, _):
            request_ids = []

            async def capture_wait_id(request):
                if request.method == "POST":
                    message = json.loads(request.content)
                    if message.get("method") == "tools/call" and message["params"][
                        "arguments"
                    ] == {"instruction": "wait"}:
                        request_ids.append(message["id"])

            http.event_hooks["request"].append(capture_wait_id)
            pending = asyncio.create_task(
                session.call_tool("run_task", {"instruction": "wait"})
            )
            try:
                with fail_after(5):
                    while not (root / "ready").exists():
                        await asyncio.sleep(0.01)
                result = await session.call_tool("get_robot_info", {})
                assert result.structuredContent["state"] == "busy"
                blocked = await session.call_tool(
                    "run_task", {"instruction": "success"}
                )
                assert blocked.isError and "already running" in blocked.content[0].text
                [request_id] = request_ids
                await session.send_notification(
                    types.ClientNotification(
                        types.CancelledNotification(
                            params=types.CancelledNotificationParams(
                                requestId=request_id
                            )
                        )
                    )
                )
                with fail_after(5):
                    while (await http.get("/healthz")).json()["busy"]:
                        await asyncio.sleep(0.01)
                [record_file] = list((root / "jobs").glob("*/job.json"))
                record = json.loads(record_file.read_text())
                assert record["status"] == "stopped" and not record["forced_stop"]
                assert (await run_task(session, "failure"))["status"] == "failed"
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())


def test_invalid_instruction_never_launches(root):
    async def run():
        async with connect(make_app(root)) as (session, _, _, _):
            for args in (
                {},
                {"instruction": ""},
                {"instruction": " "},
                {"instruction": "x" * 20001},
            ):
                assert (await session.call_tool("run_task", args)).isError
        assert not (root / "launches.jsonl").exists()

    asyncio.run(run())


def test_mcp_requires_token_and_rejects_untrusted_host(root):
    async def run():
        app = make_app(root, mcp_allowed_hosts=["go2.internal:8780"])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=URL
            ) as http:
                assert (await http.post("/mcp", json={})).status_code == 401
                body = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
                headers = {
                    "Authorization": "Bearer " + TOKEN,
                    "Accept": "application/json, text/event-stream",
                }
                response = await http.post(
                    "/mcp", json=body, headers=headers | {"Host": "untrusted.example"}
                )
                assert response.status_code == 421
                response = await http.post(
                    "/mcp", json=body, headers=headers | {"Host": "go2.internal:8780"}
                )
                assert response.status_code == 200
                assert response.headers["Mcp-Session-Id"]

    asyncio.run(run())

# Copyright 2026 The RPent Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Exercise outbound MCP discovery and calls over HTTP and stdio."""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from rpent.dashboard.events import NullDashboardEventSink
from rpent.memory import MemoryManager
from rpent.planner.utils.http_mcp_server import HttpMcpServer
from rpent.tools import Toolkit, ToolResult, tool
from rpent.tools.mcp_client import (
    McpActionStatusConfig,
    McpClient,
    McpServerConfig,
    McpServiceError,
)


def make_toolkit(path):
    return Toolkit(
        dashboard_events=NullDashboardEventSink(), memory=MemoryManager(path)
    )


@pytest.fixture
def remote(tmp_path, monkeypatch):
    monkeypatch.setattr("rpent.utils.logging._output_dir", tmp_path / "run")
    started = threading.Event()
    release = threading.Event()
    queries = []

    @tool(readonly=True)
    def lookup(name: str) -> ToolResult:
        """Look up a place by name."""
        queries.append(name)
        return ToolResult(data={"place": name})

    @tool(readonly=True)
    def slow_lookup() -> ToolResult:
        """Wait for a mock service result."""
        started.set()
        release.wait(3)
        return ToolResult(data={"place": "late"})

    with closing(make_toolkit(tmp_path / "remote")) as toolkit:
        toolkit.add_tools((lookup, slow_lookup))
        server = HttpMcpServer(toolkit)
        try:
            yield server.start(), queries, started, release
        finally:
            release.set()
            server.stop()


def test_mcp_tools_are_namespaced_filtered_and_validated(tmp_path, remote):
    url, queries, _, _ = remote
    config = McpServerConfig(url=url, enabled_tools=["lookup"])
    with closing(make_toolkit(tmp_path / "local")) as toolkit:
        toolkit.add_mcp_tools({"floor1": config, "floor2": config})
        names = {tool.name for tool in toolkit.list_tools()}
        assert "mcp__floor1__lookup" in names and "mcp__floor2__lookup" in names
        assert "mcp__floor1__slow_lookup" not in names
        assert toolkit.execute_tool("mcp__floor1__lookup", {"name": 42}).is_error
        assert queries == []
        for tool_name in ("mcp__floor1__lookup", "mcp__floor2__lookup"):
            result = toolkit.execute_tool(tool_name, {"name": "客厅"})
            assert not result.is_error
            assert json.loads(result.data["content"][0]["text"]) == {"place": "客厅"}
        assert queries == ["客厅", "客厅"]


def test_mcp_references_keep_full_results_and_use_the_discovered_schema(
    tmp_path, monkeypatch, serve_mcp
):
    monkeypatch.setattr("rpent.utils.logging._output_dir", tmp_path / "run")
    source = FastMCP("source", stateless_http=True, json_response=True)
    sink = FastMCP("sink", stateless_http=True, json_response=True)
    payload = {"waypoints": [{"name": "广告牌"}], "metadata": "x" * 70000}
    accepted = []

    @source.tool()
    def make_plan() -> dict[str, Any]:
        return payload

    @sink.tool()
    def accept(plan: dict[str, Any], target: str) -> dict[str, bool]:
        accepted.append((plan, target))
        return {"accepted": True}

    @sink.tool()
    def hidden(plan: dict[str, Any], target: str) -> dict[str, bool]:
        raise AssertionError("The model must not call an internal tool")

    with serve_mcp(source) as source_url, serve_mcp(sink) as sink_url:
        config = {
            "source": McpServerConfig(url=source_url),
            "sink": McpServerConfig(url=sink_url, model_tools=["accept"]),
        }
        with closing(make_toolkit(tmp_path / "local")) as toolkit:
            toolkit.add_mcp_tools(config)
            plan = toolkit.execute_tool("mcp__source__make_plan", {})
            reference = {"result_id": plan.data["result_id"]}
            assert reference["result_id"] in plan.to_text()
            assert plan.to_text().endswith("[truncated]")
            call = {
                "tool_name": "mcp__sink__accept",
                "arguments": {},
                "references": {
                    "plan": reference,
                    "target": {**reference, "path": ["waypoints", 0, "name"]},
                },
            }
            internal = {**call, "tool_name": "mcp__sink__hidden"}
            assert toolkit.execute_tool("mcp_call_with_refs", internal).is_error
            invalid = {
                **call,
                "references": {**call["references"], "target": reference},
            }
            mismatch = toolkit.execute_tool("mcp_call_with_refs", invalid)
            assert "does not match its schema" in mismatch.data["error"]
            assert not accepted
            result = toolkit.execute_tool("mcp_call_with_refs", call)
            assert result.data["structured_content"] == {"accepted": True}
            assert accepted == [(payload, "广告牌")]

        with closing(make_toolkit(tmp_path / "next")) as toolkit:
            toolkit.add_mcp_tools(config)
            result = toolkit.execute_tool("mcp_call_with_refs", call)
            assert "Unknown MCP result ID" in result.data["error"]
            assert len(accepted) == 1


def test_config_only_mcp_discovers_renamed_tool_for_the_next_task(
    tmp_path, monkeypatch, serve_mcp
):
    monkeypatch.setattr("rpent.utils.logging._output_dir", tmp_path / "run")
    server = FastMCP("replaceable", stateless_http=True, json_response=True)

    @server.tool()
    def lookup(name: str) -> dict[str, str]:
        return {"place": name}

    def resolve(waypoint: int) -> dict[str, int]:
        return {"waypoint": waypoint}

    with serve_mcp(server) as url:
        config = {"map": McpServerConfig(url=url)}
        with closing(make_toolkit(tmp_path / "original")) as toolkit:
            toolkit.add_mcp_tools(config)
            assert "mcp__map__lookup" in {tool.name for tool in toolkit.list_tools()}
        server.remove_tool("lookup")
        server.add_tool(resolve)
        with closing(make_toolkit(tmp_path / "updated")) as toolkit:
            toolkit.add_mcp_tools(config)
            names = {tool.name for tool in toolkit.list_tools()}
            assert "mcp__map__resolve" in names and "mcp__map__lookup" not in names
            result = toolkit.execute_tool("mcp__map__resolve", {"waypoint": 42})
            assert result.data["structured_content"] == {"waypoint": 42}


def test_configured_action_waits_with_renamed_fields_and_a_required_id_argument(
    tmp_path, monkeypatch, serve_mcp
):
    monkeypatch.setattr("rpent.utils.logging._output_dir", tmp_path / "run")
    remote = FastMCP("custom", stateless_http=True, json_response=True)
    submissions = []
    polls = []

    @remote.tool(structured_output=False)
    def assess(target: str) -> dict[str, Any]:
        return {"prepared": target != "unknown", "opaque": [target, 42]}

    @remote.tool(structured_output=False)
    def submit(artifact: dict[str, Any]) -> dict[str, Any]:
        submissions.append(artifact)
        return {"receipt": {"serial": 27}}

    @remote.tool()
    def observe(ticket: int) -> dict[str, Any]:
        polls.append(ticket)
        return {
            "meta": {"correlation": ticket},
            "response": {"complete": len(polls) >= 2, "accepted": True},
        }

    with serve_mcp(remote) as url:
        options = McpServerConfig(
            url=url,
            read_only_tools=["observe", "assess"],
            action_status=McpActionStatusConfig(
                start_tool="submit",
                status_tool="observe",
                id_field="receipt.serial",
                status_id_field="meta.correlation",
                status_id_argument="ticket",
                terminal={"response.complete": True},
                success={"response.accepted": True},
                wait_for_terminal=True,
                completion_timeout_sec=2.0,
                poll_interval_sec=0.02,
                preflight={"device.assess": {"prepared": True}},
            ),
        )
        with closing(make_toolkit(tmp_path / "blocked")) as toolkit:
            toolkit.add_mcp_tools({"device": options})
            assert toolkit.execute_tool(
                "mcp__device__submit", {"artifact": {}}
            ).is_error
            toolkit.execute_tool("mcp__device__assess", {"target": "unknown"})
            alternative = toolkit.execute_tool(
                "mcp__device__assess", {"target": "other"}
            )
            blocked = toolkit.execute_tool(
                "mcp__device__submit",
                {"artifact": alternative.data["structured_content"]},
            )
            assert "failed for this task" in blocked.data["error"]
            assert not submissions and not polls
        with closing(make_toolkit(tmp_path / "accepted")) as toolkit:
            toolkit.add_mcp_tools({"device": options})
            result = toolkit.execute_tool(
                "mcp__device__assess", {"target": "requested"}
            )
            action = toolkit.execute_tool(
                "mcp_call_with_refs",
                {
                    "tool_name": "mcp__device__submit",
                    "arguments": {},
                    "references": {"artifact": {"result_id": result.data["result_id"]}},
                },
            )
            assert not action.is_error, action.data
            assert action.data["structured_content"] == {"receipt": {"serial": 27}}
            assert action.data["action"] == {
                "action_id": 27,
                "state": "finished",
                "ok": True,
                "status": {
                    "meta": {"correlation": 27},
                    "response": {"complete": True, "accepted": True},
                },
            }
            assert submissions == [result.data["structured_content"]]
            assert polls == [27, 27]


def test_cancel_interrupts_wait_for_mcp_tool(tmp_path, remote):
    url, _, started, release = remote
    with closing(make_toolkit(tmp_path / "local")) as toolkit:
        toolkit.add_mcp_tools(
            {"scene": McpServerConfig(url=url, enabled_tools=["slow_lookup"])}
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(toolkit.execute_tool, "mcp__scene__slow_lookup", {})
            try:
                assert started.wait(3)
                toolkit.cancel_active_and_wait()
                assert call.result(timeout=1).data["interrupted"] is True
            finally:
                release.set()


def test_stdio_mcp_discovers_tools_and_releases_its_process(tmp_path, monkeypatch):
    monkeypatch.setattr("rpent.utils.logging._output_dir", tmp_path / "run")
    repository = tmp_path / "local server"
    repository.mkdir()
    (repository / "calculator.py").write_text(
        """
import os
import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("calculator")

@mcp.tool()
def add(a: int, b: int) -> dict[str, int | str | bool]:
    return {
        "sum": a + b,
        "pid": os.getpid(),
        "argument": sys.argv[1],
        "enabled": os.environ["RPENT_TEST_FORWARD"],
        "inherited": "RPENT_TEST_PRIVATE" in os.environ,
    }

mcp.run(transport="stdio")
"""
    )
    monkeypatch.setenv("RPENT_TEST_FORWARD", "allowed")
    monkeypatch.setenv("RPENT_TEST_PRIVATE", "unrelated")
    config = McpServerConfig(
        transport="stdio",
        command=sys.executable,
        args=["calculator.py", "--one argument"],
        cwd=str(repository),
        env_from=["RPENT_TEST_FORWARD"],
    )
    with closing(make_toolkit(tmp_path / "local")) as toolkit:
        toolkit.add_mcp_tools({"calculator": config})
        tool = "mcp__calculator__add"
        assert tool in {declaration.name for declaration in toolkit.list_tools()}
        result = toolkit.execute_tool(tool, {"a": 17, "b": 25})
        assert not result.is_error, result.data
        values = result.data["structured_content"]
        assert values["sum"] == 42
        assert values["argument"] == "--one argument"
        assert values["enabled"] == "allowed" and not values["inherited"]
    with pytest.raises(ProcessLookupError):
        os.kill(values["pid"], 0)


def test_stdio_startup_timeout_releases_unresponsive_child(tmp_path):
    pid_file = tmp_path / "pid"
    program = (
        "import os, time; from pathlib import Path; "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    config = McpServerConfig(
        transport="stdio",
        command=sys.executable,
        args=["-c", program],
        startup_timeout_sec=1.0,
    )
    with pytest.raises(McpServiceError, match="startup failed"):
        McpClient("slow", config)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)

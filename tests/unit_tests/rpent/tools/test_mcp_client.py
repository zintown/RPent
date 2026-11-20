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

import pytest

from rpent.dashboard.events import NullDashboardEventSink
from rpent.memory import MemoryManager
from rpent.planner.utils.http_mcp_server import HttpMcpServer
from rpent.tools import Toolkit, ToolResult, tool
from rpent.tools.mcp_client import McpClient, McpServerConfig, McpServiceError


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

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

"""Exercise outbound MCP discovery and calls over loopback HTTP."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from rpent.dashboard.events import NullDashboardEventSink
from rpent.memory import MemoryManager
from rpent.planner.utils.http_mcp_server import HttpMcpServer
from rpent.tools import Toolkit, ToolResult, tool
from rpent.tools.mcp_client import McpServerConfig


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

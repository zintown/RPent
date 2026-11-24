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

"""An in-memory HTTP boundary for environments where loopback sockets are blocked."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


@pytest.fixture(autouse=True)
def tool_output_dir(tmp_path, monkeypatch):
    """Provide the run directory expected by shared tool-description templates."""
    monkeypatch.setattr("rpent.utils.logging._output_dir", tmp_path / "run")


@pytest.fixture
def mcp_navigation(serve_mcp):
    """Provide MCP-only backends to exercise RPent's ordinary CLI interface."""
    astar = FastMCP("test-astar", stateless_http=True, json_response=True)
    sim = FastMCP("test-mujoco", stateless_http=True, json_response=True)
    state = SimpleNamespace(plans={}, submissions=[])
    readonly = ToolAnnotations(readOnlyHint=True)
    waypoint = {
        "id": "wp_01",
        "name": "广告牌",
        "stand_pose": {"x": 1, "y": 0, "yaw": 0},
    }

    @astar.tool(annotations=readonly)
    def list_waypoints() -> dict[str, Any]:
        return {"ok": True, "waypoints": [waypoint]}

    @astar.tool(annotations=readonly)
    def plan_route(start: dict, goal: str) -> dict[str, Any]:
        assert goal == "wp_01"
        state.plans["plan_1"] = {
            "legs": [{"path": [[start["x"], start["y"]], [1.0, 0.0]]}]
        }
        return {"ok": True, "plan_id": "plan_1"}

    @sim.tool(annotations=readonly)
    def get_pose() -> dict[str, Any]:
        return {"ok": True, "x": 0.0, "y": 0.0, "yaw": 0.0, "fallen": False}

    @sim.tool()
    def follow_plan(plan_id: str) -> dict[str, Any]:
        state.submissions.append(state.plans[plan_id])
        return {
            "ok": True,
            "navigation_completed": True,
            "walk_id": "walk_1",
            "pose": {"x": 1.0, "y": 0.0, "yaw": 0.0},
        }

    @sim.tool(annotations=readonly)
    def get_walk_status() -> dict[str, Any]:
        return {"state": "finished"}

    with serve_mcp(astar) as astar_url, serve_mcp(sim) as sim_url:
        yield SimpleNamespace(astar_url=astar_url, sim_url=sim_url, state=state)

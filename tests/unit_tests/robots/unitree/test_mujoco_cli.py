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

"""Exercise the Unitree MCP CLI with an offline API model."""

import argparse
import io
import json
import sys

import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from rpent.dashboard.events import NullDashboardEventSink
from rpent.robots import get_robot_spec
from rpent.tools.mcp_client import McpServerConfig


@pytest.mark.parametrize("enabled,allowlist", [(False, None), (True, [])])
def test_unitree_requires_a_configured_service_with_tools(tmp_path, enabled, allowlist):
    spec = get_robot_spec("unitree")
    parser = argparse.ArgumentParser()
    spec.add_cli_args(parser, False)
    args = parser.parse_args(["--task", "查询位姿"])
    args.mcp_servers = {
        "empty": McpServerConfig(
            url="http://localhost:1/mcp", enabled=enabled, enabled_tools=allowlist
        )
    }
    with pytest.raises(ValueError, match="requires enabled tools"):
        spec.parse_config(args)
    assert spec.init_runtime(args, tmp_path, NullDashboardEventSink(), None) == ([], {})


def test_public_cli_discovers_services_plans_and_follows_the_exact_route(
    tmp_path, mcp_navigation, navigation_services, monkeypatch
):
    from rpent.cli import main as cli

    services = navigation_services
    services.state.polls_before_finish = 0
    config_path = tmp_path / "mcp.toml"
    config_path.write_text(
        f'[mcp_servers.astar]\nurl = "{mcp_navigation.astar_url}"\n'
        'enabled_tools = ["list_waypoints", "plan_route"]\n'
        f'[mcp_servers.mujoco]\nurl = "{mcp_navigation.sim_url}"\n'
        'enabled_tools = ["get_pose", "follow_plan", "get_walk_status"]\n'
    )
    calls = []

    def payload(messages, name):
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == name
        ]
        content = returns[-1].content
        return json.loads(content[0] if isinstance(content, list) else content)[
            "structured_content"
        ]

    async def stream(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert {
            "mcp__astar__list_waypoints",
            "mcp__astar__plan_route",
            "mcp__mujoco__get_pose",
            "mcp__mujoco__follow_plan",
        } <= names
        assert "dis_move" not in names
        assert "follow_navigation" not in names
        index = len(calls)
        if index == 0:
            name, arguments = "mcp__astar__list_waypoints", {}
        elif index == 1:
            name, arguments = "mcp__mujoco__get_pose", {}
        elif index == 2:
            pose = payload(messages, "mcp__mujoco__get_pose")
            name, arguments = (
                "mcp__astar__plan_route",
                {
                    "start": {key: pose[key] for key in ("x", "y", "yaw")},
                    "goal": "wp_01",
                },
            )
        elif index == 3:
            route = payload(messages, "mcp__astar__plan_route")
            name, arguments = "mcp__mujoco__follow_plan", {"plan_id": route["plan_id"]}
        else:
            assert payload(messages, "mcp__mujoco__follow_plan")["navigation_completed"]
            name, arguments = "finish", {"status": "success", "summary": "到达广告牌"}
        calls.append(name)
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments))}

    class TerminalInput(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(
        "rpent.planner.base.build_api_model",
        lambda *args, **kwargs: FunctionModel(stream_function=stream),
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput())
    out = tmp_path / "run"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rpent",
            "--robot",
            "unitree",
            "--mcp-config",
            str(config_path),
            "--planner",
            "api",
            "--model",
            "openai-chat:offline-navigation",
            "--reasoning-effort",
            "none",
            "--max-turns",
            "5",
            "--planner-timeout-s",
            "10",
            "--task",
            "去广告牌",
            "--output-dir",
            str(out),
            "--memory-dir",
            str(tmp_path / "memory"),
        ],
    )
    assert cli.main() == 0
    assert len(services.state.submissions) == 1
    assert services.state.submissions[0]["legs"][0]["path"] == [[0.0, 0.0], [1.0, 0.0]]
    record = json.loads(next(out.glob("transcript_unitree_*.json")).read_text())
    assert record["error"] is None
    assert record["finish"]["status"] == "success"
    assert record["validation_scope"] == "unitree_mcp"

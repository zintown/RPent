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

"""MuJoCo runtime selection and CLI policy chain with an offline API model."""

import argparse
import io
import json
import socket
import sys
import threading
import time
from contextlib import closing, contextmanager

import pytest
import uvicorn
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from rpent.dashboard.events import NullDashboardEventSink
from rpent.robots import get_robot_spec, get_toolkit


def args_for(tmp_path, *flags):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(tmp_path))
    parser.add_argument("--memory-profile", default=None)
    parser.add_argument("--memory-dir", default=None)
    get_robot_spec("unitree").add_cli_args(parser, False)
    return parser.parse_args(
        [
            "--task",
            "去广告牌",
            "--unitree-mode",
            "mujoco",
            "--nav-endpoint",
            "http://nav.test",
            "--sim-endpoint",
            "http://sim.test",
            *flags,
        ]
    )


def use_services(monkeypatch, services):
    monkeypatch.setattr(
        "robots.unitree.navigation_client.NavigationClient",
        lambda *args, **kwargs: services.nav,
    )
    monkeypatch.setattr(
        "robots.unitree.simulation_client.SimulationClient",
        lambda *args, **kwargs: services.sim,
    )


def test_mode_selects_navigation_runtime_and_prompt(
    tmp_path, navigation_services, monkeypatch
):
    from robots.unitree.mujoco_toolkit import UnitreeMujocoToolkit
    from robots.unitree.robot_spec import get_toolkit

    use_services(monkeypatch, navigation_services)
    spec = get_robot_spec("unitree")
    args = args_for(tmp_path)
    config = spec.parse_config(args)
    daemons, runtime = spec.init_runtime(args, tmp_path, NullDashboardEventSink(), None)
    assert daemons == []
    assert set(runtime) == {"nav", "sim"}
    toolkit = get_toolkit(
        runtime_kwargs=runtime, dashboard_events=NullDashboardEventSink(), config=config
    )
    try:
        assert isinstance(toolkit, UnitreeMujocoToolkit)
        system = spec.prompts.render("system", variables=config.prompt_vars)
        user = spec.prompts.render("user", variables=config.prompt_vars)
        assert "follow_navigation" in system
        assert "dis_move" not in system
        assert "SIMULATION SERVICE" in user
        assert config.task_desc["validation_scope"] == "mujoco_navigation"
    finally:
        toolkit.close()


@pytest.mark.parametrize(
    "flags",
    [
        ["--robot-index", "1"],
        ["--task", "   "],
        ["--memory-profile", "hf"],
        ["--navigation-timeout-s", "nan"],
        ["--navigation-timeout-s", "0"],
        ["--nav-endpoint", "socket://localhost:1"],
    ],
)
def test_invalid_mujoco_configuration_has_no_network_io(
    tmp_path, flags, navigation_services
):
    with pytest.raises(ValueError):
        get_robot_spec("unitree").parse_config(args_for(tmp_path, *flags))
    assert not navigation_services.state.requests


def test_startup_map_mismatch_closes_both_created_clients(
    tmp_path, navigation_services, monkeypatch
):
    use_services(monkeypatch, navigation_services)
    navigation_services.state.sim_version = "different"
    closed = []
    monkeypatch.setattr(navigation_services.nav, "close", lambda: closed.append("nav"))
    monkeypatch.setattr(navigation_services.sim, "close", lambda: closed.append("sim"))
    with pytest.raises(ValueError, match="map_id/map_version"):
        get_robot_spec("unitree").init_runtime(
            args_for(tmp_path), tmp_path, NullDashboardEventSink(), None
        )
    assert closed == ["nav", "sim"]


def test_toolkit_construction_failure_closes_runtime_clients(
    tmp_path, navigation_services, monkeypatch
):
    from robots.unitree.robot_spec import get_toolkit

    def fail(**kwargs):
        raise RuntimeError("state store unavailable")

    monkeypatch.setattr("robots.unitree.mujoco_toolkit.UnitreeMujocoToolkit", fail)
    closed = []
    monkeypatch.setattr(navigation_services.nav, "close", lambda: closed.append("nav"))
    monkeypatch.setattr(navigation_services.sim, "close", lambda: closed.append("sim"))
    config = get_robot_spec("unitree").parse_config(args_for(tmp_path))
    with pytest.raises(RuntimeError, match="state store unavailable"):
        get_toolkit(
            runtime_kwargs={
                "nav": navigation_services.nav,
                "sim": navigation_services.sim,
            },
            dashboard_events=NullDashboardEventSink(),
            config=config,
        )
    assert closed == ["nav", "sim"]


def test_selected_empty_runtime_does_not_contact_services(
    tmp_path, navigation_services, monkeypatch
):
    use_services(monkeypatch, navigation_services)
    result = get_robot_spec("unitree").init_runtime(
        args_for(tmp_path), tmp_path, NullDashboardEventSink(), set()
    )
    assert result == ([], {})
    assert not navigation_services.state.requests


@contextmanager
def serve_mcp(mcp):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(mcp.streamable_http_app(), log_level="warning")
        )
        worker = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                assert time.monotonic() < deadline and worker.is_alive()
                time.sleep(0.01)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            server.should_exit = True
            worker.join(5)
            assert not worker.is_alive()


@pytest.fixture(params=[False, True], ids=["http", "http-with-scene-mcp"])
def navigation_args(request, tmp_path):
    if not request.param:
        yield []
        return

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("scene", stateless_http=True, json_response=True)

    @mcp.tool()
    def lookup(name: str) -> dict[str, str]:
        """Look up a named scene object."""
        return {"name": name, "waypoint_id": "wp_01"}

    with serve_mcp(mcp) as url:
        path = tmp_path / "mcp.toml"
        path.write_text(
            f'[mcp_servers.scene]\nurl = "{url}"\nenabled_tools = ["lookup"]\n',
            encoding="utf-8",
        )
        yield ["--mcp-config", str(path)]


def test_mcp_mode_uses_discovered_services_to_execute_the_saved_plan(
    tmp_path,
    navigation_loopback,
):
    from robots.unitree.mcp.astar.server import create_astar_mcp
    from robots.unitree.mcp.mujoco.server import create_mujoco_mcp
    from rpent.tools.mcp_client import McpClient, McpServerConfig

    services = navigation_loopback
    services.state.polls_before_finish = 0
    astar = create_astar_mcp(services.nav, host="127.0.0.1", port=0)
    with (
        serve_mcp(astar) as astar_url,
        closing(McpClient("astar", McpServerConfig(url=astar_url))) as planner,
    ):
        sim = create_mujoco_mcp(services.sim, planner, host="127.0.0.1", port=0)
        with serve_mcp(sim) as sim_url:
            args = args_for(tmp_path, "--unitree-mode", "mcp")
            args.memory_profile = "local"
            args.memory_dir = str(tmp_path / "memory")
            args.mcp_servers = {
                "astar": McpServerConfig(
                    url=astar_url, enabled_tools=["list_waypoints", "plan_route"]
                ),
                "mujoco": McpServerConfig(
                    url=sim_url, enabled_tools=["get_pose", "follow_plan"]
                ),
            }
            spec = get_robot_spec("unitree")
            config = spec.parse_config(args)
            sink = NullDashboardEventSink()
            assert spec.init_runtime(args, tmp_path, sink, None) == ([], {})
            with closing(
                get_toolkit(
                    "unitree", runtime_kwargs={}, dashboard_events=sink, config=config
                )
            ) as toolkit:
                toolkit.add_mcp_tools(args.mcp_servers)
                names = {tool.name for tool in toolkit.list_tools()}
                assert not {"plan_navigation", "follow_navigation"} & names
                pose = toolkit.execute_tool("mcp__mujoco__get_pose", {}).data[
                    "structured_content"
                ]
                route = toolkit.execute_tool(
                    "mcp__astar__plan_route",
                    {
                        "start": {key: pose[key] for key in ("x", "y", "yaw")},
                        "goal": "wp_01",
                    },
                ).data["structured_content"]
                arguments = {"plan_id": route["plan_id"]}
                walk = toolkit.execute_tool("mcp__mujoco__follow_plan", arguments)
                assert walk.data["structured_content"]["navigation_completed"] is True
                assert toolkit.execute_tool(
                    "mcp__mujoco__follow_plan", arguments
                ).is_error
    assert len(services.state.submissions) == 1
    assert services.state.submissions[0]["legs"][0]["path"] == [[0.0, 0.0], [1.0, 0.0]]


def test_public_cli_plans_follows_and_records_native_simulation_result(
    tmp_path, navigation_loopback, navigation_args, monkeypatch
):
    from rpent.cli import main as cli

    services = navigation_loopback
    use_services(monkeypatch, services)
    services.state.polls_before_finish = 0
    requests = []

    def returned_payload(messages, name):
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == name
        ]
        content = returns[-1].content
        return json.loads(content[0] if isinstance(content, list) else content)

    async def stream(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert {
            "list_navigation_goals",
            "plan_navigation",
            "follow_navigation",
        } <= names
        assert "execute_command" not in names
        index = len(requests)
        requests.append(messages)
        if navigation_args:
            assert "mcp__scene__lookup" in names
            if index == 0:
                yield {
                    0: DeltaToolCall(
                        name="mcp__scene__lookup",
                        json_args=json.dumps({"name": "广告牌"}),
                    )
                }
                return
            if index == 1:
                assert returned_payload(messages, "mcp__scene__lookup")[
                    "structured_content"
                ] == {"name": "广告牌", "waypoint_id": "wp_01"}
            index -= 1
        if index == 0:
            name, arguments = "list_navigation_goals", {}
        elif index == 1:
            name, arguments = "plan_navigation", {"goals": ["广告牌"]}
        elif index == 2:
            payload = returned_payload(messages, "plan_navigation")
            name, arguments = "follow_navigation", {"plan_id": payload["plan_id"]}
        else:
            name, arguments = "finish", {"status": "success", "summary": "到达广告牌"}
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments))}

    class TerminalInput(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(
        "rpent.planner.base.build_api_model",
        lambda *args, **kwargs: FunctionModel(stream_function=stream),
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rpent",
            "--robot",
            "unitree",
            "--unitree-mode",
            "mujoco",
            "--nav-endpoint",
            services.nav.endpoint,
            "--sim-endpoint",
            services.sim.endpoint,
            *navigation_args,
            "--planner",
            "api",
            "--model",
            "openai-chat:offline-navigation",
            "--reasoning-effort",
            "none",
            "--max-turns",
            "5" if navigation_args else "4",
            "--planner-timeout-s",
            "5",
            "--task",
            "去广告牌",
            "--output-dir",
            str(tmp_path / "run"),
        ],
    )
    assert cli.main() == 0
    assert len(services.state.submissions) == 1
    record = json.loads(
        next((tmp_path / "run").glob("transcript_unitree_*.json")).read_text()
    )
    assert record["error"] is None
    assert record["finish"]["status"] == "success"
    assert record["unitree_mode"] == "mujoco"
    if navigation_args:
        assert any(
            entry.get("name") == "mcp__scene__lookup" for entry in record["messages"]
        )
    states = json.loads((tmp_path / "run" / "states.json").read_text())
    assert states["steps"][-1]["state"]["completed_navigation_goals"] == 1
    assert list((tmp_path / "run" / "navigation" / "plans").glob("*.json"))
    assert list((tmp_path / "run" / "navigation" / "walks").glob("*.json"))

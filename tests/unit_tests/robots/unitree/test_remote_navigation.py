"""Exercise RPent's workflow against the remote A* and simulation contracts."""

from __future__ import annotations

import shlex
import sys
from contextlib import closing
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
import tomlkit
from mcp.server.fastmcp import FastMCP

from robots.remote_navigation.navigation import (
    Navigation,
    NavigationServices,
    read_evidence,
)
from rpent.mcp.task.config import load_deployment
from rpent.mcp.task.discovery import check_robot
from rpent.mcp.task.runner import TaskRunner
from rpent.mcp.task.tools import create_task_mcp
from rpent.tools.mcp_client import McpClient, McpServerConfig, McpServiceError
from rpent.tools.toolkit import ToolCancelled

ROOT = Path(__file__).resolve().parents[4]
MAP = {"map_id": "room1110", "map_version": "test.1"}
POSE = {"x": 1.0, "y": 2.0, "yaw": 0.2, "fallen": False}
GOALS = ["广告牌", "柜子"]


@pytest.fixture
def backends(serve_mcp, tmp_path, monkeypatch):
    astar = FastMCP("robot-astar", stateless_http=True, json_response=True)
    sim = FastMCP("robot-sim", stateless_http=True, json_response=True)
    state = SimpleNamespace(
        plan=None,
        submissions=[],
        polls=0,
        polled=Event(),
        stops=0,
        walking=False,
        simulator_running=True,
        sim_map=MAP,
        behavior="complete",
    )
    monkeypatch.setattr("robots.remote_navigation.navigation.POLL_INTERVAL_S", 0.01)

    @astar.tool()
    def service_health() -> dict[str, Any]:
        return {"ok": True, **MAP}

    @astar.tool()
    def plan_route(
        start_x: float, start_y: float, start_yaw: float, goals: list[str]
    ) -> dict[str, Any]:
        assert (start_x, start_y, start_yaw) == (POSE["x"], POSE["y"], POSE["yaw"])
        state.plan = {
            "ok": True,
            **MAP,
            "frame": "map",
            "units": {"length": "meter", "angle": "radian"},
            "legs": [
                {
                    "path": [[start_x, start_y], [2.0 + index, 2.0]],
                    "goal": {"name": goal, "stand_pose": {"yaw": 0.7}},
                    "headings": [0.0],
                }
                for index, goal in enumerate(goals)
            ],
            "warnings": ["keep provider fields when handing off a plan"],
        }
        return state.plan

    @sim.tool()
    def sim_status() -> dict[str, Any]:
        return {
            "ok": True,
            **state.sim_map,
            "simulator_running": state.simulator_running,
            "pose": POSE if state.simulator_running else None,
            "other_controllers": [],
            "walk": {"state": "running"} if state.walking else None,
        }

    @sim.tool()
    def get_pose() -> dict[str, Any]:
        return {"ok": True, **POSE}

    @sim.tool()
    def walk_start(plan: dict[str, Any]) -> dict[str, Any]:
        state.submissions.append(plan)
        state.walking = True
        if state.behavior == "lost_reply":
            raise RuntimeError("submission reply lost")
        return {"ok": True, "walk_id": "walk_1"}

    @sim.tool()
    def walk_status() -> dict[str, Any]:
        state.polls += 1
        state.polled.set()
        if state.behavior in {"complete", "missed_leg"} and state.polls > 1:
            state.walking = False
        return {
            "ok": True,
            "walk_id": "another_walk" if state.behavior == "foreign_walk" else "walk_1",
            "state": "running" if state.walking else "finished",
            "pose": POSE,
            "result": None
            if state.walking
            else {
                "ok": True,
                "legs": [
                    {"reached": state.behavior != "missed_leg" or index > 0}
                    for index in range(len(state.plan["legs"]))
                ],
            },
        }

    @sim.tool()
    def walk_stop() -> dict[str, Any]:
        state.stops += 1
        state.walking = False
        return {"ok": True}

    with serve_mcp(astar) as astar_url, serve_mcp(sim) as sim_url:
        config = tomlkit.parse((ROOT / "mcp.toml").read_text())
        config["mcp_servers"]["astar"]["url"] = astar_url
        config["mcp_servers"]["sim"]["url"] = sim_url
        path = tmp_path / "mcp.toml"
        path.write_text(tomlkit.dumps(config))
        yield load_deployment(path), state, path


def test_workflow_readiness_filters_map_mismatch_and_stopped_sim(backends):
    deployment, state, _ = backends
    assert check_robot(deployment, "idle").capabilities[0].status == "ready"
    state.sim_map = {**MAP, "map_version": "test.2"}
    capability = check_robot(deployment, "idle").capabilities[0]
    assert capability.status == "unavailable" and "map mismatch" in capability.reason
    state.simulator_running = False
    capability = check_robot(deployment, "idle").capabilities[0]
    assert (
        capability.status == "unavailable"
        and "simulator is stopped" in capability.reason
    )
    assert not state.submissions


@pytest.mark.parametrize(
    ("behavior", "expected"),
    [("complete", "succeeded"), ("missed_leg", "failed")],
)
def test_handoff_preserves_full_route_and_verifies_each_leg(
    backends, tmp_path, behavior, expected
):
    deployment, state, _ = backends
    state.behavior = behavior
    nav = Navigation(
        NavigationServices.from_deployment(deployment), tmp_path, lambda: None
    )
    result = nav.navigate(GOALS)
    assert state.submissions == [state.plan]
    assert state.polls >= 2
    assert result["walk_id"] == "walk_1"
    assert result["state"] == expected
    evidence = read_evidence(tmp_path, {})
    assert evidence.data["navigation"]["completed_navigation_goals"] == (
        2 if expected == "succeeded" else 1
    )
    assert bool(evidence.error) == (expected == "failed")


@pytest.mark.parametrize("behavior", ["foreign_walk", "lost_reply"])
def test_uncertain_action_blocks_submissions_and_leaves_other_walk_alone(
    backends, tmp_path, behavior
):
    deployment, state, _ = backends
    state.behavior = behavior
    nav = Navigation(
        NavigationServices.from_deployment(deployment), tmp_path, lambda: None
    )
    assert nav.navigate(GOALS)["state"] == "requires_attention"
    assert read_evidence(tmp_path, {}).requires_attention
    with pytest.raises(McpServiceError, match="Verify the prior walk"):
        nav.navigate(GOALS)
    assert len(state.submissions) == 1
    assert state.stops == 0


def test_cancel_confirms_owned_walk_stopped(backends, tmp_path):
    deployment, state, _ = backends
    state.behavior = "wait_for_stop"

    def check_cancelled():
        if state.polls:
            raise ToolCancelled("test cancelled")

    nav = Navigation(
        NavigationServices.from_deployment(deployment), tmp_path, check_cancelled
    )
    with pytest.raises(ToolCancelled, match="test cancelled"):
        nav.navigate(GOALS)
    evidence = read_evidence(tmp_path, {})
    assert not evidence.requires_attention
    assert evidence.data["navigation"]["state"] == "stopped"
    assert evidence.data["navigation"]["walk_state"] == "finished"
    assert state.stops == 1 and not state.walking


def test_can_bind_another_planner_with_the_same_contract(backends, tmp_path):
    _, state, config_path = backends
    config = tomlkit.parse(config_path.read_text())
    config["mcp_servers"]["other"] = config["mcp_servers"].pop("astar")
    config["service_adapters"]["other"] = config["service_adapters"].pop("astar")
    config["capabilities"]["map_navigation"]["bindings"]["planner"] = "other"
    config_path.write_text(tomlkit.dumps(config))
    deployment = load_deployment(config_path)
    nav = Navigation(
        NavigationServices.from_deployment(deployment), tmp_path, lambda: None
    )
    assert nav.navigate(GOALS)["state"] == "succeeded"
    assert state.submissions == [state.plan]


CHILD = """
import json
import os
import sys
from pathlib import Path

from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rpent.cli import main as cli
from rpent.planner import base

output = Path(os.environ["RPENT_DISPATCH_OUTPUT_DIR"])
os.environ["RPENT_TASK_OUTPUT_DIR"] = str(output)
called = False

async def model(messages, info):
    global called
    names = {tool.name for tool in info.function_tools}
    assert "navigate" in names and "mcp__sim__walk_start" not in names
    if called:
        name, arguments = "finish", {"status": "success", "summary": "stub planner completed"}
    else:
        name, arguments = "navigate", {"goals": ["广告牌", "柜子"]}
        called = True
    yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments))}

base.build_api_model = lambda *args, **kwargs: FunctionModel(stream_function=model)
sys.argv = [
    "rpent", "--mcp-config", os.environ["RPENT_DISPATCH_MCP_CONFIG"],
    "--planner", "api", "--model", "openai-chat:offline-navigation",
    "--reasoning-effort", "none", "--max-turns", "4", "--planner-timeout-s", "12",
    "--task", sys.argv[1], "--output-dir", str(output),
    "--memory-dir", str(output / "memory"),
]
raise SystemExit(cli.main())
"""


@pytest.fixture
def task_runner(backends, tmp_path, monkeypatch):
    deployment, state, _ = backends
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    child = tmp_path / "child.py"
    child.write_text(CHILD)
    (tmp_path / "run_rpent.sh").write_text(
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(child))} "$@"\n'
    )
    jobs = tmp_path / "jobs"
    with closing(TaskRunner(tmp_path, jobs, deployment)) as runner:
        yield runner, state


def test_dsh_task_mcp_delivers_bindings_to_worker_and_returns_evidence(
    task_runner, serve_mcp
):
    runner, state = task_runner
    # A missed leg must override success reported by the model.
    state.behavior = "missed_leg"
    server = create_task_mcp(lambda: runner, deployment=runner.deployment)
    with (
        serve_mcp(server) as url,
        closing(
            McpClient("rpent", McpServerConfig(url=url, tool_timeout_sec=20))
        ) as client,
    ):
        info = client.call_tool("get_robot_info", {}).structuredContent
        assert info["capabilities"][0]["status"] == "ready"
        reply = client.call_tool("run_task", {"instruction": "去广告牌再去柜子"})
        result = reply.structuredContent
        assert result["status"] == "failed"
        assert result["navigation"] is not None, result
        assert result["navigation"]["walk_id"] == "walk_1"
        assert result["navigation"]["completed_navigation_goals"] == 1
    assert state.submissions == [state.plan]


def test_task_runner_cancellation_stops_remote_walk(task_runner):
    runner, state = task_runner
    state.behavior = "wait_for_stop"
    job = runner.start("前往广告牌再去柜子")
    assert state.polled.wait(10), "worker did not poll its remote walk"
    runner.cancel(job)
    result = runner.wait(job, 10)
    assert result["status"] == "stopped", result
    assert result["navigation"]["walk_id"] == "walk_1"
    assert result["navigation"]["walk_state"] == "finished"
    assert state.stops == 1 and not state.walking

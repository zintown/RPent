"""Exercise RPent's workflow against the remote A* and simulation contracts."""

from __future__ import annotations

import json
import shlex
import sys
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
import tomlkit
from mcp import types
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
from rpent.robots.base import get_robot_spec
from rpent.tools.mcp_client import McpClient, McpServerConfig, McpServiceError
from rpent.tools.mcp_registry import McpServiceRegistry
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
        sim_tool_lists=0,
        polled=Event(),
        stops=0,
        walking=False,
        simulator_running=True,
        sim_map=MAP,
        behavior="complete",
        plan_format="legacy",
    )
    monkeypatch.setattr("robots.remote_navigation.navigation.POLL_INTERVAL_S", 0.01)

    @astar.tool()
    def service_health() -> dict[str, Any]:
        return {"ok": True, **MAP}

    @astar.tool()
    def list_waypoints() -> dict[str, Any]:
        return {"ok": True, "waypoints": []}

    @astar.tool()
    def list_scene_graph() -> dict[str, Any]:
        return {"ok": True, "nodes": []}

    @astar.tool()
    def plan_route(
        start_x: float, start_y: float, start_yaw: float, goals: list[str]
    ) -> dict[str, Any]:
        assert (start_x, start_y, start_yaw) == (POSE["x"], POSE["y"], POSE["yaw"])
        if state.plan_format == "replacement":
            state.plan = {
                "ok": True,
                **MAP,
                "route": {
                    "segments": [
                        {"target": goal, "positions": [[start_x, start_y], [2.0, 2.0]]}
                        for goal in goals
                    ],
                },
                "format": "replacement",
            }
            return state.plan
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
        if state.behavior == "rejected":
            return {"ok": False, "error": "requested walk was rejected"}
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
        legs = (
            state.plan["route"]["segments"]
            if state.plan_format == "replacement"
            else state.plan["legs"]
        )
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
                    for index in range(len(legs))
                ],
            },
        }

    @sim.tool()
    def walk_stop() -> dict[str, Any]:
        state.stops += 1
        state.walking = False
        return {"ok": True}

    list_tools = sim._mcp_server.request_handlers[types.ListToolsRequest]

    async def count_tools(request):
        state.sim_tool_lists += 1
        return await list_tools(request)

    sim._mcp_server.request_handlers[types.ListToolsRequest] = count_tools
    with serve_mcp(astar) as astar_url, serve_mcp(sim) as sim_url:
        config = tomlkit.parse(
            (ROOT / "robots" / "remote_navigation" / "mcp.toml").read_text()
        )
        config["mcp_servers"]["astar"]["url"] = astar_url
        config["mcp_servers"]["sim"]["url"] = sim_url
        path = tmp_path / "mcp.toml"
        path.write_text(tomlkit.dumps(config))
        yield load_deployment(path), state, path


@pytest.fixture
def make_navigation(tmp_path):
    with ExitStack() as stack:

        def create(deployment, check_cancelled=lambda: None):
            registry = stack.enter_context(
                closing(McpServiceRegistry(deployment.servers))
            )
            return Navigation(
                NavigationServices.from_deployment(deployment),
                registry,
                tmp_path,
                check_cancelled,
            )

        yield create


def test_workflow_readiness_filters_map_mismatch_and_stopped_sim(backends):
    deployment, state, _ = backends
    assert deployment.servers["sim"].model_tools == []
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


def test_additional_workflow_uses_the_same_readiness_registry(backends, monkeypatch):
    _, state, path = backends
    config = tomlkit.parse(path.read_text())
    config["capabilities"]["inspection"] = {
        "description": "Read simulation state",
        "bindings": {"simulator": "sim"},
    }
    path.write_text(tomlkit.dumps(config))

    spec = get_robot_spec("remote_navigation")

    def configure(deployment):
        configured = spec.task_execution.configure_deployment(deployment)
        service = configured.capabilities["inspection"].bindings["simulator"]

        def check(registry):
            result = registry.connect(service).call_tool("sim_status", {})
            if result.structuredContent["ok"] is not True:
                return "inspection is unavailable"
            return None

        execution = configured.execution
        return replace(
            configured,
            execution=replace(
                execution,
                capability_checks={**execution.capability_checks, "inspection": check},
            ),
        )

    monkeypatch.setattr(
        "rpent.mcp.task.config.get_robot_spec",
        lambda _name: replace(
            spec,
            task_execution=replace(spec.task_execution, configure_deployment=configure),
        ),
    )
    info = check_robot(load_deployment(path), "idle")
    assert {cap.name: cap.status for cap in info.capabilities} == {
        "map_navigation": "ready",
        "inspection": "ready",
    }
    assert state.sim_tool_lists == 1


@pytest.mark.parametrize(
    ("behavior", "expected"),
    [("complete", "succeeded"), ("missed_leg", "failed")],
)
def test_handoff_preserves_full_route_and_verifies_each_leg(
    backends, tmp_path, behavior, expected, make_navigation
):
    deployment, state, _ = backends
    state.behavior = behavior
    nav = make_navigation(deployment)
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
    backends, tmp_path, behavior, make_navigation
):
    deployment, state, _ = backends
    state.behavior = behavior
    nav = make_navigation(deployment)
    assert nav.navigate(GOALS)["state"] == "requires_attention"
    assert read_evidence(tmp_path, {}).requires_attention
    with pytest.raises(McpServiceError, match="Verify the prior walk"):
        nav.navigate(GOALS)
    assert len(state.submissions) == 1
    assert state.stops == 0


def test_cancel_confirms_owned_walk_stopped(backends, tmp_path, make_navigation):
    deployment, state, _ = backends
    state.behavior = "wait_for_stop"

    def check_cancelled():
        if state.polls:
            raise ToolCancelled("test cancelled")

    nav = make_navigation(deployment, check_cancelled)
    with pytest.raises(ToolCancelled, match="test cancelled"):
        nav.navigate(GOALS)
    evidence = read_evidence(tmp_path, {})
    assert not evidence.requires_attention
    assert evidence.data["navigation"]["state"] == "stopped"
    assert evidence.data["navigation"]["walk_state"] == "finished"
    assert state.stops == 1 and not state.walking


def test_can_bind_another_planner_with_the_same_contract(backends, make_navigation):
    _, state, config_path = backends
    config = tomlkit.parse(config_path.read_text())
    config["mcp_servers"]["other"] = config["mcp_servers"].pop("astar")
    config["service_adapters"]["other"] = config["service_adapters"].pop("astar")
    config["capabilities"]["map_navigation"]["bindings"]["planner"] = "other"
    config_path.write_text(tomlkit.dumps(config))
    deployment = load_deployment(config_path)
    nav = make_navigation(deployment)
    assert nav.navigate(GOALS)["state"] == "succeeded"
    assert state.submissions == [state.plan]


CHILD_SETUP = """
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rpent.cli import main as cli
from rpent.planner import base

output = Path(os.environ["RPENT_DISPATCH_OUTPUT_DIR"])
os.environ["RPENT_TASK_OUTPUT_DIR"] = str(output)
calls = 0
"""

CHILD_LAUNCH = """
base.build_api_model = lambda *args, **kwargs: FunctionModel(stream_function=model)
sys.argv = [
    "rpent", "--mcp-config", os.environ["RPENT_DISPATCH_MCP_CONFIG"],
    "--planner", "api", "--model", "openai-chat:offline-navigation",
    "--reasoning-effort", "none", "--max-turns", os.getenv("TEST_RPENT_MAX_TURNS", "12"),
    "--planner-timeout-s", "12",
    "--task", sys.argv[1], "--output-dir", str(output),
    "--memory-dir", str(output / "memory"),
]
raise SystemExit(cli.main())
"""

NAVIGATION_MODEL = """
async def model(messages, info):
    global calls
    names = {tool.name for tool in info.function_tools}
    assert {"navigate", "mcp__sim__sim_status"} <= names
    assert "mcp__sim__walk_start" not in names and "mcp__detector__detect" not in names
    if calls == 0:
        name, arguments = "mcp__sim__sim_status", {}
    elif calls == 1:
        name, arguments = "navigate", {"goals": ["广告牌", "柜子"]}
    else:
        name, arguments = "finish", {"status": "success", "summary": "stub planner completed"}
    calls += 1
    yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments))}
"""

DIRECT_MODEL = """
def result_from(messages, name):
    for message in reversed(messages):
        for part in reversed(message.parts):
            if isinstance(part, ToolReturnPart) and part.tool_name == name:
                content = part.content
                return json.loads(content[0] if isinstance(content, list) else content)
    raise AssertionError(f"Missing result for {name}")

async def model(messages, info):
    global calls
    names = {tool.name for tool in info.function_tools}
    assert {
        "mcp_call_with_refs", "mcp__sim__get_pose", "mcp__astar__plan_route",
        "mcp__sim__walk_start", "mcp__sim__walk_status",
    } <= names
    assert "navigate" not in names
    if calls == 0:
        name, arguments = "mcp__sim__get_pose", {}
    elif calls == 1:
        pose_id = result_from(messages, "mcp__sim__get_pose")["result_id"]
        name, arguments = "mcp_call_with_refs", {
            "tool_name": "mcp__astar__plan_route",
            "arguments": {"goals": ["广告牌", "柜子"]},
            "references": {
                f"start_{field}": {"result_id": pose_id, "path": [field]}
                for field in ("x", "y", "yaw")
            },
        }
    elif calls == 2:
        plan_id = result_from(messages, "mcp_call_with_refs")["result_id"]
        name, arguments = "mcp_call_with_refs", {
            "tool_name": "mcp__sim__walk_start",
            "arguments": {},
            "references": {"plan": {"result_id": plan_id}},
        }
    else:
        name, arguments = "finish", {"status": "success", "summary": "stub completed"}
        if calls == 3:
            submitted = result_from(messages, "mcp_call_with_refs")
            if "action" not in submitted and submitted.get("structured_content", {}).get("walk_id"):
                name, arguments = "mcp__sim__walk_status", {}
        else:
            status = result_from(messages, "mcp__sim__walk_status")["structured_content"]
            if status["state"] == "running":
                await asyncio.sleep(0.05)
                name, arguments = "mcp__sim__walk_status", {}
    calls += 1
    yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments))}
"""


def launch_runner(tmp_path, monkeypatch, deployment, model, **kwargs):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    child = tmp_path / "child.py"
    child.write_text(CHILD_SETUP + model + CHILD_LAUNCH)
    (tmp_path / "run_rpent.sh").write_text(
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(child))} "$@"\n'
    )
    return closing(TaskRunner(tmp_path, tmp_path / "jobs", deployment, **kwargs))


@pytest.fixture
def task_runner(backends, tmp_path, monkeypatch):
    _, state, path = backends
    config = tomlkit.parse(path.read_text())
    config["mcp_servers"]["sim"]["model_tools"] = ["sim_status"]
    config["mcp_servers"]["detector"] = {
        "url": "http://127.0.0.1:1/mcp",
        "enabled_tools": ["detect"],
        "startup_timeout_sec": 0.5,
    }
    config["capabilities"]["detection"] = {
        "description": "Detect objects",
        "tools": ["detector.detect"],
    }
    path.write_text(tomlkit.dumps(config))
    with launch_runner(
        tmp_path, monkeypatch, load_deployment(path), NAVIGATION_MODEL
    ) as runner:
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
        assert info["state"] == "idle"
        assert {item["name"]: item["status"] for item in info["capabilities"]} == {
            "map_navigation": "ready",
            "detection": "unavailable",
        }
        assert set(info["service_errors"]) == {"detector"}
        reply = client.call_tool("run_task", {"instruction": "去广告牌再去柜子"})
        result = reply.structuredContent
        assert result["status"] == "failed"
        assert result["navigation"] is not None, result
        assert result["navigation"]["walk_id"] == "walk_1"
        assert result["navigation"]["completed_navigation_goals"] == 1
    assert state.submissions == [state.plan]
    # One list request within get_robot_info and one within the task process.
    assert state.sim_tool_lists == 2


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


@pytest.fixture
def direct_task_runner(backends, tmp_path, monkeypatch, request):
    remote, state, path = backends
    config = tomlkit.parse((ROOT / "mcp.toml").read_text())
    for name, server in remote.servers.items():
        config["mcp_servers"][name]["url"] = server.url
    action = config["mcp_servers"]["sim"]["action_status"]
    action["poll_interval_sec"] = 0.03
    action["completion_timeout_sec"] = 3.0
    runner_options = {}
    if getattr(request, "param", None) == "budget":
        action["wait_for_terminal"] = False
        monkeypatch.setenv("TEST_RPENT_MAX_TURNS", "3")
        runner_options["stop_timeout_s"] = 0.4
    path.write_text(tomlkit.dumps(config))
    with launch_runner(
        tmp_path, monkeypatch, load_deployment(path), DIRECT_MODEL, **runner_options
    ) as runner:
        yield runner, state


def test_generic_readiness_checks_the_same_map_from_both_services(direct_task_runner):
    runner, state = direct_task_runner
    assert check_robot(runner.deployment, "idle").capabilities[0].status == "ready"
    state.sim_map = {**MAP, "map_version": "test.2"}
    capability = check_robot(runner.deployment, "idle").capabilities[0]
    assert capability.status == "unavailable"
    assert (
        "sim.sim_status.map_version differs from astar.service_health.map_version"
        in capability.reason
    )
    assert not state.submissions


@pytest.mark.parametrize("plan_format", ["legacy", "replacement"])
def test_generic_dsh_task_passes_the_complete_plan(
    direct_task_runner, serve_mcp, plan_format
):
    runner, state = direct_task_runner
    state.plan_format = plan_format
    server = create_task_mcp(lambda: runner, deployment=runner.deployment)
    with (
        serve_mcp(server) as url,
        closing(
            McpClient("rpent", McpServerConfig(url=url, tool_timeout_sec=20))
        ) as client,
    ):
        info = client.call_tool("get_robot_info", {}).structuredContent
        assert info["capabilities"][0]["status"] == "ready", info
        reply = client.call_tool("run_task", {"instruction": "去广告牌再去柜子"})
        result = reply.structuredContent
        assert result["status"] == "succeeded", result
        action = result["evidence"]["mcp_actions"]["sim"]
        assert (
            action["action_id"] == "walk_1" and action["status"]["state"] == "finished"
        )
    assert state.submissions == [state.plan]
    assert state.sim_tool_lists == 2
    job = next(runner.jobs_dir.glob("run-*/job.json"))
    assert json.loads(job.read_text())["stats"]["turns_used"] == 4


@pytest.mark.parametrize(
    ("behavior", "outcome"),
    [
        ("missed_leg", "failed"),
        ("lost_reply", "requires_attention"),
        ("rejected", "failed"),
    ],
)
def test_generic_task_evidence_overrides_inaccurate_model_summary(
    direct_task_runner, behavior, outcome
):
    runner, state = direct_task_runner
    state.behavior = behavior
    job = runner.start("前往广告牌再去柜子")
    result = runner.wait(job, 20)
    assert result["status"] == outcome, result
    assert len(state.submissions) == 1


def test_generic_cancellation_reports_unconfirmed_stop(direct_task_runner):
    runner, state = direct_task_runner
    state.behavior = "wait_for_stop"
    job = runner.start("前往广告牌再去柜子")
    assert state.polled.wait(10), "worker did not poll its remote walk"
    runner.cancel(job)
    result = runner.wait(job, 10)
    assert result["status"] == "requires_attention", result
    assert state.stops == 0 and state.walking


@pytest.mark.parametrize("direct_task_runner", ["budget"], indirect=True)
def test_late_receipt_releases_the_gate_without_replaying_motion(
    direct_task_runner, serve_mcp
):
    runner, state = direct_task_runner
    state.behavior = "wait_for_stop"
    job = runner.start("前往广告牌再去柜子")
    assert runner.wait(job, 10)["status"] == "requires_attention"
    state.walking = False
    state.behavior = "foreign_walk"
    server = create_task_mcp(lambda: runner, deployment=runner.deployment)
    with (
        serve_mcp(server) as url,
        closing(McpClient("rpent", McpServerConfig(url=url))) as client,
    ):
        status = client.call_tool("get_robot_info", {}).structuredContent
        assert status["state"] == "requires_attention"
    state.behavior = "complete"
    runner.close()
    with closing(
        TaskRunner(runner.repo_root, runner.jobs_dir, runner.deployment)
    ) as recovered:
        assert not recovered.health()["requires_attention"]
        record = json.loads((job.directory / "job.json").read_text())
        assert record["status"] == "budget_exhausted"
        assert record["reconciliation"]["previous_status"] == "requires_attention"
        assert record["evidence"]["mcp_actions"]["sim"]["status"]["walk_id"] == "walk_1"
    assert len(state.submissions) == 1

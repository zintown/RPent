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

"""Check route identity, preview, arrival and stopping through the MCP adapters."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from rpent.tools.mcp_client import McpClient, McpServerConfig
from rpent.tools.toolkit import ToolCancelled


@pytest.fixture
def clients(mcp_navigation):
    with (
        closing(
            McpClient("astar", McpServerConfig(url=mcp_navigation.astar_url))
        ) as astar,
        closing(
            McpClient("mujoco", McpServerConfig(url=mcp_navigation.sim_url))
        ) as sim,
    ):
        yield astar, sim


def plan(clients, goal="广告牌"):
    astar, sim = clients
    pose = sim.call_tool("get_pose", {}).structuredContent
    result = astar.call_tool(
        "plan_route",
        {"start": {key: pose[key] for key in ("x", "y", "yaw")}, "goal": goal},
    )
    assert not result.isError, result
    return result.structuredContent["plan_id"]


def follow(clients, plan_id):
    return clients[1].call_tool("follow_plan", {"plan_id": plan_id})


def test_preserves_exact_plan_and_prevents_duplicate_submission(
    clients, navigation_services
):
    navigation_services.state.pose.update(x=0.2, y=0.4, yaw=0.5)
    plan_id = plan(clients, {"x": 2, "y": 3, "yaw": 1.57})
    route = clients[0].call_tool("get_plan", {"plan_id": plan_id}).structuredContent
    result = follow(clients, plan_id)
    assert not result.isError
    assert result.structuredContent["navigation_completed"] is True
    assert navigation_services.state.submissions == [route]
    assert route["start"] == {"x": 0.2, "y": 0.4, "yaw": 0.5}
    assert route["legs"][0]["goal"]["stand_pose"]["yaw"] == 1.57
    assert follow(clients, plan_id).isError
    assert len(navigation_services.state.submissions) == 1


@pytest.mark.parametrize(
    "condition", ["map", "fallen", "controller", "simulator", "departure"]
)
def test_invalid_environment_never_submits_motion(
    clients, navigation_services, condition
):
    state = navigation_services.state
    plan_id = plan(clients)
    if condition == "map":
        state.sim_version = "different"
    elif condition == "fallen":
        state.pose["fallen"] = True
    elif condition == "controller":
        state.other_controllers = ["another controller"]
    elif condition == "departure":
        state.pose["x"] = 0.5
    else:
        state.simulator_running = False
    assert follow(clients, plan_id).isError
    assert not state.submissions


def test_multigoal_is_rejected_at_the_one_goal_planning_boundary(
    clients, navigation_services
):
    result = clients[0].call_tool(
        "plan_route", {"start": {"x": 0, "y": 0, "yaw": 0}, "goal": ["广告牌", "厕所"]}
    )
    assert result.isError
    assert all(path != "/plan" for _, _, path, _ in navigation_services.state.requests)


def test_failed_preview_blocks_following_same_plan(clients, navigation_services):
    plan_id = plan(clients)
    navigation_services.state.preview_failed = True
    assert clients[1].call_tool("preview_plan", {"plan_id": plan_id}).isError
    assert follow(clients, plan_id).isError
    assert not navigation_services.state.submissions


def test_preview_accepts_top_level_success_without_nested_ok(
    clients, navigation_services, monkeypatch
):
    monkeypatch.setattr(
        navigation_services.sim,
        "preview",
        lambda *args, **kwargs: {"ok": True, "result": {"legs": [{"reached": True}]}},
    )
    plan_id = plan(clients)
    assert not clients[1].call_tool("preview_plan", {"plan_id": plan_id}).isError
    assert not follow(clients, plan_id).isError


@pytest.mark.parametrize("error_source", ["reported", "pose"])
@pytest.mark.parametrize(
    ("position_error", "accepted"), [(0.2, True), (0.25, True), (0.251, False)]
)
def test_position_tolerance_checks_reported_error_and_final_pose(
    clients, navigation_services, error_source, position_error, accepted
):
    def mutate(result):
        if error_source == "reported":
            result["result"]["legs"][0]["position_error"] = position_error
        else:
            result["pose"]["x"] += position_error

    navigation_services.state.final_mutator = mutate
    result = follow(clients, plan(clients))
    assert result.isError == (not accepted), result
    if accepted:
        assert result.structuredContent["navigation_completed"] is True


@pytest.mark.parametrize("failure", ["skipped", "position", "yaw", "fallen"])
def test_finished_does_not_mean_success(clients, navigation_services, failure):
    if failure == "skipped":
        navigation_services.state.failed = True
    else:

        def mutate(result):
            if failure == "position":
                result["pose"]["x"] += 1
            elif failure == "yaw":
                result["result"]["legs"][0]["yaw_error"] = 0.3
            else:
                result["fell"] = True

        navigation_services.state.final_mutator = mutate
    result = follow(clients, plan(clients))
    assert result.isError


def test_uncertain_acceptance_is_not_retried_or_blindly_stopped(
    clients, navigation_services
):
    state = navigation_services.state
    plan_id = plan(clients)
    state.submit_timeout = True
    assert follow(clients, plan_id).isError
    assert follow(clients, plan_id).isError
    assert len(state.submissions) == 1
    assert not state.stopped


def test_timeout_stops_the_owned_walk(clients, navigation_services):
    state = navigation_services.state
    state.hold = True
    result = follow(clients, plan(clients))
    assert result.isError
    assert "did not finish" in result.content[0].text
    assert state.stopped and not state.running
    assert len(state.submissions) == 1


def test_disconnecting_a_client_stops_the_owned_walk(clients, navigation_services):
    state = navigation_services.state
    state.hold = True
    plan_id = plan(clients)
    cancelled = threading.Event()

    def check_cancelled():
        if cancelled.is_set():
            raise ToolCancelled("walk cancelled")

    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(
            clients[1].call_tool,
            "follow_plan",
            {"plan_id": plan_id},
            check_cancelled=check_cancelled,
        )
        assert state.started.wait(1)
        cancelled.set()
        with pytest.raises(ToolCancelled):
            active.result(timeout=2)
    clients[1].close()
    deadline = time.monotonic() + 0.9
    while state.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state.stopped and not state.running


def test_lost_ownership_never_stops_another_walk(clients, navigation_services):
    state = navigation_services.state
    state.hold = True
    state.status_walk_id = "someone_else"
    result = follow(clients, plan(clients))
    assert result.isError and "replaced walk" in result.content[0].text
    assert not state.stopped

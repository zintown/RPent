# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

"""Check the two service requests and the operator boundary of a short A* move."""

import io
import json
import math
import sys

import httpx
import pytest

from robots.unitree import real_astar_check
from robots.unitree.navigation_client import NavigationClient
from robots.unitree.real_client import RealRobotClient

IDENTITY = {"map_id": "site", "map_version": "v1"}
START = {"x": 1.0, "y": 2.0, "yaw": math.pi / 2}
REQUEST_ID = "27b1bdf4-e894-42c7-b44f-175c13bd35d2"


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


def make_plan(goal, *, detour=False, warning=False):
    path = [[START["x"], START["y"]], [goal["x"], goal["y"]]]
    if detour:
        path.insert(1, [1.1, 2.15])
    length = sum(math.dist(a, b) for a, b in zip(path, path[1:]))
    return {
        "ok": True,
        **IDENTITY,
        "frame": "map",
        "units": {"length": "meter", "angle": "radian"},
        "start": START,
        "total_length": length,
        "warnings": ["start outside the corridor"] if warning else [],
        "legs": [
            {
                "goal": {"stand_pose": goal},
                "path": path,
                "length": length,
                "headings": [
                    math.atan2(b[1] - a[1], b[0] - a[0]) for a, b in zip(path, path[1:])
                ],
            }
        ],
    }


def argv(out, *, goal_y=2.3, execute=True):
    flags = [
        "--nav-endpoint",
        "http://astar.test",
        "--map-id",
        "site",
        "--map-version",
        "v1",
        "--start",
        "1",
        "2",
        str(math.pi / 2),
        "--goal",
        "1",
        str(goal_y),
        "--output-dir",
        str(out),
        "--real-endpoint",
        "http://go2.test:9001",
        "--robot-index",
        "1",
    ]
    return flags + (["--execute"] if execute else [])


def test_confirmed_short_plan_uses_actual_gateway_client_and_polling(
    tmp_path, monkeypatch
):
    calls = []
    queued = {}
    out = tmp_path / "check"

    def send(request):
        if request.url.host == "astar.test":
            path = request.url.path
            calls.append(path)
            if path == "/health":
                return httpx.Response(200, json={"ok": True, **IDENTITY})
            assert request.method == "POST" and path == "/plan"
            payload = json.loads(request.content)
            assert payload["start"] == START
            return httpx.Response(200, json=make_plan(payload["goals"][0]))

        assert request.url.host == "go2.test" and request.url.path == "/call"
        payload = json.loads(request.content)
        method = payload["method"]
        calls.append(method)
        if method == "healthz":
            result = {
                "robot_index": 1,
                "status": "online",
                "connected": True,
                "heartbeat_healthy": True,
                "connected_at_unix": 1795060800,
            }
        elif method == "env.execute_command":
            assert (out / "plan.json").is_file()
            assert payload["kwargs"]["robot_index"] == 1
            assert payload["idempotency_key"] == payload["request_id"]
            command = payload["args"][0]
            assert command["type"] == "move_relative"
            assert command["payload"] == pytest.approx(
                {"dx_m": 0.3, "dy_m": 0, "arrival_turn_deg": 0}
            )
            queued.update(
                request_id=REQUEST_ID,
                client_request_id=payload["request_id"],
                robot_index=1,
                operation="move_relative",
                status="queued",
                result_available=False,
                outcome_known=False,
                result=None,
                error=None,
            )
            result = queued
        else:
            assert (
                method == "env.get_request"
                and payload["kwargs"]["request_id"] == REQUEST_ID
            )
            result = dict(
                queued,
                status="completed",
                result_available=True,
                outcome_known=True,
                result={
                    "type": "command_result",
                    "robot_index": 1,
                    "request_id": REQUEST_ID,
                    "status": "completed",
                    "completed": True,
                    "dry_run": False,
                    "exit_code": 0,
                    "reason": "completed",
                    "result": {
                        "success": True,
                        "arrival_phase_started": True,
                        "final_state_available": True,
                        "stop_ack": True,
                    },
                },
            )
        return httpx.Response(200, json={"ok": True, "result": result})

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        monkeypatch.setattr(
            real_astar_check,
            "NavigationClient",
            lambda endpoint: NavigationClient(endpoint, client=http),
        )
        monkeypatch.setattr(
            real_astar_check,
            "RealRobotClient",
            lambda endpoint, robot_index: RealRobotClient(
                endpoint, robot_index=robot_index, client=http
            ),
        )
        monkeypatch.setattr(sys, "stdin", TerminalInput("MOVE\n"))
        assert real_astar_check.main(argv(out)) == 0

    assert calls == [
        "/health",
        "/plan",
        "healthz",
        "env.execute_command",
        "env.get_request",
    ]
    assert json.loads((out / "execution.json").read_text())["request_id"] == REQUEST_ID
    assert json.loads((out / "real/latest.json").read_text())["stop_confirmed"] is True


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("preview", 0),
        ("declined", 2),
        ("detour", 1),
        ("long", 1),
        ("warning", 1),
        ("wrong-map", 1),
    ],
)
def test_diagnostic_does_not_contact_go2_without_a_confirmed_eligible_route(
    tmp_path, monkeypatch, case, expected
):
    def send(request):
        assert request.url.host == "astar.test"
        if request.url.path == "/health":
            identity = (
                IDENTITY | {"map_version": "other"} if case == "wrong-map" else IDENTITY
            )
            return httpx.Response(200, json={"ok": True, **identity})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json=make_plan(
                body["goals"][0], detour=case == "detour", warning=case == "warning"
            ),
        )

    def unexpected_gateway(*args, **kwargs):
        pytest.fail("diagnostic contacted a robot before confirming an eligible plan")

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        monkeypatch.setattr(
            real_astar_check,
            "NavigationClient",
            lambda endpoint: NavigationClient(endpoint, client=http),
        )
        monkeypatch.setattr(real_astar_check, "RealRobotClient", unexpected_gateway)
        monkeypatch.setattr(sys, "stdin", TerminalInput("\n"))
        flags = argv(
            tmp_path / "check",
            goal_y=2.8 if case == "long" else 2.3,
            execute=case != "preview",
        )
        assert real_astar_check.main(flags) == expected

    assert not (tmp_path / "check/execution.json").exists()

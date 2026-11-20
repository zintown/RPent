# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

"""Verify the operator-confirmed diagnostic uses A* and Go2 through MCP."""

import io
import json
import math
import sys
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from robots.unitree.mcp.real import astar_check

REQUEST_ID = "27b1bdf4-e894-42c7-b44f-175c13bd35d2"


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def diagnostic_config(tmp_path, mcp_navigation, serve_mcp, monkeypatch):
    moves = []
    robot = FastMCP("test-go2", stateless_http=True, json_response=True)

    @robot.tool()
    def get_robot_status() -> dict[str, Any]:
        return {"robot_index": 1, "status": "online"}

    @robot.tool()
    def move_relative(
        dx_m: float, dy_m: float, arrival_turn_deg: float
    ) -> dict[str, Any]:
        moves.append((dx_m, dy_m, arrival_turn_deg))
        return {"status": "completed", "request_id": REQUEST_ID}

    monkeypatch.delenv("RPENT_TASK_OUTPUT_DIR", raising=False)
    with serve_mcp(robot) as real_url:
        path = tmp_path / "diagnostic.toml"
        path.write_text(
            f'[mcp_servers.astar]\nurl = "{mcp_navigation.astar_url}"\n'
            f'[mcp_servers.real]\nurl = "{real_url}"\n'
        )
        yield path, moves


def argv(config, out, *, goal_y=2.3, version="v1", execute=True):
    flags = [
        "--mcp-config",
        str(config),
        "--map-id",
        "test_room",
        "--map-version",
        version,
        "--start",
        "1",
        "2",
        str(math.pi / 2),
        "--goal",
        "1",
        str(goal_y),
        "--output-dir",
        str(out),
    ]
    return flags + (["--execute"] if execute else [])


def test_confirmed_short_plan_sends_one_body_frame_move(
    tmp_path, diagnostic_config, monkeypatch, navigation_services
):
    config, moves = diagnostic_config
    out = tmp_path / "check"
    monkeypatch.setattr(sys, "stdin", TerminalInput("MOVE\n"))
    assert astar_check.main(argv(config, out)) == 0
    assert len(moves) == 1
    assert moves[0] == pytest.approx((0.3, 0, 0))
    plan = json.loads((out / "plan.json").read_text())
    assert plan["legs"][0]["path"] == [[1.0, 2.0], [1.0, 2.3]]
    assert json.loads((out / "execution.json").read_text())["request_id"] == REQUEST_ID
    assert [path for _, _, path, _ in navigation_services.state.requests] == [
        "/waypoints",
        "/plan",
    ]


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
def test_diagnostic_requires_an_eligible_route_and_operator_confirmation(
    tmp_path, diagnostic_config, monkeypatch, navigation_services, case, expected
):
    config, moves = diagnostic_config
    out = tmp_path / "check"

    def mutate(plan):
        if case == "detour":
            plan["legs"][0]["path"].insert(1, [1.1, 2.15])
            plan["legs"][0]["headings"].append(math.pi / 2)
        elif case == "warning":
            plan["warnings"].append("start outside the corridor")

    navigation_services.state.plan_mutator = mutate
    monkeypatch.setattr(sys, "stdin", TerminalInput("\n"))
    flags = argv(
        config,
        out,
        goal_y=2.8 if case == "long" else 2.3,
        version="other" if case == "wrong-map" else "v1",
        execute=case != "preview",
    )
    assert astar_check.main(flags) == expected
    assert not moves
    assert not (out / "execution.json").exists()

# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

"""Check A* and a physical Go2 with an operator-measured map pose and one move."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from robots.unitree.http_client import ServiceError, finite_number, map_identity
from robots.unitree.mcp.real.client import relative_command
from robots.unitree.navigation_client import CoordinateGoal
from rpent.evaluation import write_json_atomic
from rpent.tools.mcp_client import McpClient, McpServiceError, load_mcp_servers

_MAX_CHECK_DISTANCE_M = 0.5
_ENDPOINT_TOLERANCE_M = 0.01  # The A* API rounds returned path coordinates.


def short_move(
    plan: dict[str, Any], start: dict[str, float], goal: CoordinateGoal
) -> dict[str, float]:
    """Convert one short straight route to body-frame motion for the measured start."""
    if plan.get("warnings"):
        raise ServiceError(
            "A* returned a route warning: " + ", ".join(plan["warnings"])
        )
    leg = plan["legs"][0]
    path = leg["path"]
    if len(path) != 2:
        raise ServiceError("the physical check requires a single straight A* segment")
    origin = (start["x"], start["y"])
    stand = leg["goal"]["stand_pose"]
    target = (stand["x"], stand["y"])
    if (
        math.dist(path[0], origin) > _ENDPOINT_TOLERANCE_M
        or math.dist(path[1], target) > _ENDPOINT_TOLERANCE_M
        or math.dist(target, (goal.x, goal.y)) > _ENDPOINT_TOLERANCE_M
    ):
        raise ServiceError(
            "planned segment endpoints do not match the measured start and goal"
        )
    delta_x, delta_y = target[0] - origin[0], target[1] - origin[1]
    if not 0 < math.hypot(delta_x, delta_y) <= _MAX_CHECK_DISTANCE_M + 1e-9:
        raise ServiceError("the A* check requires a nonzero move of at most 0.5 m")
    yaw = start["yaw"]
    forward = math.cos(yaw) * delta_x + math.sin(yaw) * delta_y
    left = -math.sin(yaw) * delta_x + math.cos(yaw) * delta_y
    return relative_command(forward, left, 0)["payload"]


def _call(client: McpClient, tool: str, **arguments: Any) -> dict[str, Any]:
    result = client.call_tool(tool, arguments)
    if result.isError:
        detail = next((part.text for part in result.content if part.type == "text"), "")
        raise ServiceError(f"{client.name}/{tool}: {detail}")
    if not isinstance(result.structuredContent, dict):
        raise ServiceError(f"{client.name}/{tool} did not return a structured result")
    return result.structuredContent


def main(argv: list[str] | None = None) -> int:
    """Plan first and execute one relative command only on site confirmation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mcp-config",
        type=Path,
        default=Path(os.environ.get("RPENT_MCP_CONFIG") or "mcp.toml"),
    )
    parser.add_argument("--map-id", required=True)
    parser.add_argument("--map-version", required=True)
    parser.add_argument(
        "--start", nargs=3, type=float, metavar=("X", "Y", "YAW"), required=True
    )
    parser.add_argument(
        "--goal", nargs=2, type=float, metavar=("X", "Y"), required=True
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Confirm and send the displayed move via the Go2 service.",
    )
    args = parser.parse_args(argv)

    try:
        if args.execute and not sys.stdin.isatty():
            raise ValueError("--execute requires an on-site operator at a terminal")
        servers = load_mcp_servers(args.mcp_config)
        for name in ("astar", "real") if args.execute else ("astar",):
            if name not in servers or not servers[name].enabled:
                raise ValueError(f"enable the {name!r} MCP service in --mcp-config")
        start = {
            key: finite_number(value, f"start.{key}")
            for key, value in zip(("x", "y", "yaw"), args.start, strict=True)
        }
        if not -math.pi <= start["yaw"] <= math.pi:
            raise ValueError("start yaw must be measured in radians in [-pi, pi]")
        goal = CoordinateGoal(x=args.goal[0], y=args.goal[1])
        identity = (args.map_id, args.map_version)
        output_dir = args.output_dir or (
            Path("output/real-integration/astar-checks") / uuid4().hex[:12]
        )
        output_dir = output_dir.resolve()

        with closing(McpClient("astar", servers["astar"])) as nav:
            if map_identity(_call(nav, "list_waypoints")) != identity:
                raise ServiceError(
                    "the A* map identity does not match the measured start"
                )
            route = _call(
                nav, "plan_route", start=start, goal=goal.model_dump(exclude_none=True)
            )
            plan = _call(nav, "get_plan", plan_id=route["plan_id"])
            if map_identity(plan) != identity:
                raise ServiceError("the A* map changed during planning")
        output_dir.mkdir(parents=True, exist_ok=False)
        plan_path = write_json_atomic(output_dir / "plan.json", plan)
        print(f"A* plan: {plan_path}", flush=True)
        move = short_move(plan, start, goal)
        check = {
            "map_id": args.map_id,
            "map_version": args.map_version,
            "pose_source": "operator_measured",
            "start": start,
            "path": plan["legs"][0]["path"],
            "distance_m": math.hypot(move["dx_m"], move["dy_m"]),
            "relative_move": move,
        }
        write_json_atomic(output_dir / "check.json", check)
        print(json.dumps(check, ensure_ascii=False, indent=2), flush=True)
        if not args.execute:
            return 0
        os.environ["RPENT_TASK_OUTPUT_DIR"] = str(output_dir)
        with closing(McpClient("real", servers["real"])) as real:
            robot = _call(real, "get_robot_status")
            confirmation = input(
                f"确认本体编号 {robot['robot_index']} 位于上述实测起点与朝向、"
                "现场路径清空且操作员可以手动停止后，输入 MOVE 执行一次："
            )
            if confirmation.strip() != "MOVE":
                print("已取消执行。")
                return 2
            result = _call(real, "move_relative", **move)
        write_json_atomic(output_dir / "execution.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("已中断。请核对现场停车和 real/latest.json。", file=sys.stderr)
        return 130
    except (EOFError, OSError, ValueError, ServiceError, McpServiceError) as exc:
        print(f"真机 A* 测试失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

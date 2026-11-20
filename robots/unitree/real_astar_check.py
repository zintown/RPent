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
from pathlib import Path
from typing import Any
from uuid import uuid4

from robots.unitree.http_client import (
    ServiceError,
    finite_number,
    map_identity,
    validate_endpoint,
)
from robots.unitree.navigation_client import CoordinateGoal, NavigationClient
from robots.unitree.real_client import (
    RealRobotClient,
    relative_command,
    validate_robot_index,
)
from robots.unitree.real_toolkit import RealRobotToolkit
from rpent.dashboard.events import NullDashboardEventSink
from rpent.evaluation import write_json_atomic
from rpent.memory import MemoryManager

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


def _execute(
    endpoint: str,
    robot_index: int,
    move: dict[str, float],
    output_dir: Path,
    timeout_s: float,
) -> dict[str, Any]:
    client = RealRobotClient(endpoint, robot_index=robot_index)
    try:
        toolkit = RealRobotToolkit(
            real=client,
            dashboard_events=NullDashboardEventSink(),
            memory=MemoryManager(output_dir / "memory"),
            state_output_dir=output_dir,
            action_timeout_s=timeout_s,
        )
    except Exception:
        client.close()
        raise
    try:
        return toolkit.execute_tool("move_relative", move).data
    finally:
        toolkit.close()


def main(argv: list[str] | None = None) -> int:
    """Plan first and execute one relative command only on site confirmation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--nav-endpoint",
        default=os.environ.get("RPENT_NAV_ENDPOINT", "http://127.0.0.1:8765"),
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
    parser.add_argument(
        "--real-endpoint", default=os.environ.get("RPENT_REAL_ENDPOINT")
    )
    parser.add_argument(
        "--robot-index", type=int, default=os.environ.get("RPENT_ROBOT_INDEX")
    )
    parser.add_argument("--action-timeout-s", type=float, default=60.0)
    args = parser.parse_args(argv)

    try:
        if args.execute:
            if not args.real_endpoint or args.robot_index is None:
                raise ValueError("--execute requires --real-endpoint and --robot-index")
            validate_endpoint(args.real_endpoint)
            validate_robot_index(args.robot_index)
            if args.real_endpoint.rstrip("/").endswith("/call"):
                raise ValueError("--real-endpoint is a base URL; omit /call")
            if not math.isfinite(args.action_timeout_s) or args.action_timeout_s <= 0:
                raise ValueError("--action-timeout-s must be finite and positive")
            if not sys.stdin.isatty():
                raise ValueError("--execute requires an on-site operator at a terminal")
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

        nav = NavigationClient(args.nav_endpoint)
        try:
            if map_identity(nav.health()) != identity:
                raise ServiceError(
                    "the A* map identity does not match the measured start"
                )
            plan = nav.plan(start, [goal])
            if map_identity(plan) != identity:
                raise ServiceError("the A* map changed during planning")
        finally:
            nav.close()
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
        confirmation = input(
            f"确认 {args.real_endpoint} 的本体编号 {args.robot_index} 位于上述实测起点与朝向、"
            "现场路径清空且操作员可以手动停止后，输入 MOVE 执行一次："
        )
        if confirmation.strip() != "MOVE":
            print("已取消执行。")
            return 2
        result = _execute(
            args.real_endpoint,
            args.robot_index,
            move,
            output_dir,
            args.action_timeout_s,
        )
        write_json_atomic(output_dir / "execution.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("error") else 0
    except KeyboardInterrupt:
        print("已中断。请核对现场停车和 real/latest.json。", file=sys.stderr)
        return 130
    except (EOFError, OSError, ValueError, ServiceError) as exc:
        print(f"真机 A* 测试失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

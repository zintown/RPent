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

"""Expose external MuJoCo route execution over Streamable HTTP MCP."""

from __future__ import annotations

import argparse
import math
from contextlib import ExitStack
from functools import partial
from typing import Any

from anyio import CancelScope, Lock, fail_after, sleep, to_thread
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request

from robots.unitree.http_client import ServiceError, finite_number, map_identity
from robots.unitree.simulation_client import (
    SimulationClient,
    simulator_can_move,
    validate_walk_arrival,
)
from rpent.tools.mcp_client import McpClient, McpServerConfig


def _read_plan(planning: McpClient, plan_id: str) -> dict[str, Any]:
    result = planning.call_tool("get_plan", {"plan_id": plan_id})
    if result.isError:
        detail = next((part.text for part in result.content if part.type == "text"), "")
        raise ServiceError(f"could not load A* plan {plan_id}: {detail}")
    route = result.structuredContent
    if not isinstance(route, dict) or route.get("ok") is not True:
        raise ServiceError(f"A* returned an invalid route for {plan_id}")
    return route


async def _owned_status(simulation: SimulationClient, walk_id: str) -> dict[str, Any]:
    status = await to_thread.run_sync(simulation.get_walk_status)
    if status.get("walk_id") != walk_id:
        raise ServiceError(f"simulation replaced walk {walk_id}")
    return status


async def _wait_for_walk(
    simulation: SimulationClient, walk_id: str, request: Request | None = None
) -> dict[str, Any]:
    while True:
        status = await _owned_status(simulation, walk_id)
        if status["state"] == "finished":
            return status
        if request is not None and await request.is_disconnected():
            raise ServiceError(f"caller disconnected during walk {walk_id}")
        await sleep(0.2)


async def _stop_walk(simulation: SimulationClient, walk_id: str) -> None:
    with fail_after(15, shield=True):
        status = await _owned_status(simulation, walk_id)
        if status["state"] == "running":
            await to_thread.run_sync(simulation.stop_walk)
            await _wait_for_walk(simulation, walk_id)


def create_mujoco_mcp(
    simulation: SimulationClient,
    planning: McpClient,
    *,
    host: str,
    port: int,
    walk_timeout_s: float = 120.0,
) -> FastMCP:
    """Build a service that executes a saved A* MCP route by plan ID."""
    if not math.isfinite(walk_timeout_s) or walk_timeout_s <= 0:
        raise ValueError("walk timeout must be finite and positive")
    server = FastMCP(
        "unitree-mujoco",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    walk_lock = Lock()
    submitted: set[str] = set()
    failed_previews: set[str] = set()

    async def load_plan(plan_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        route = await to_thread.run_sync(_read_plan, planning, plan_id)
        health = await to_thread.run_sync(simulation.health)
        if map_identity(route) != map_identity(health):
            raise ServiceError("saved plan uses a different map or map version")
        return route, health

    @server.tool(annotations=readonly)
    async def get_pose() -> dict[str, Any]:
        """Read the simulator's current x, y and yaw for A* planning."""
        return await to_thread.run_sync(simulation.get_pose)

    @server.tool(annotations=readonly)
    async def get_navigation_status() -> dict[str, Any]:
        """Check the live motion controller and whether its map matches A*."""
        health = await to_thread.run_sync(simulation.health)
        identity = map_identity(health)
        waypoints = await to_thread.run_sync(planning.call_tool, "list_waypoints", {})
        data = waypoints.structuredContent
        if waypoints.isError or data is None or data.get("ok") is not True:
            raise ServiceError("A* map could not be verified")
        return {
            "ready": simulator_can_move(health)
            and not walk_lock.locked()
            and not (health.get("control") or {}).get("requires_recovery")
            and identity == map_identity(data),
            "map_id": identity[0],
            "map_version": identity[1],
        }

    @server.tool(annotations=readonly)
    async def get_walk_status() -> dict[str, Any]:
        """Read the latest simulator walk and its execution evidence."""
        return await to_thread.run_sync(simulation.get_walk_status)

    @server.tool(annotations=readonly)
    async def preview_plan(plan_id: str) -> dict[str, Any]:
        """Preview a saved A* plan in an independent simulation."""
        async with walk_lock:
            route, _ = await load_plan(plan_id)
            failed_previews.add(plan_id)
            result = await to_thread.run_sync(
                partial(simulation.preview, route, timeout_s=walk_timeout_s)
            )
            preview = result.get("result")
            if isinstance(preview, dict) and (
                preview.get("ok") is False
                or any(
                    leg.get("reached") is False
                    for leg in preview.get("legs", [])
                    if isinstance(leg, dict)
                )
            ):
                raise ServiceError("preview did not confirm arrival")
            failed_previews.discard(plan_id)
            return result

    @server.tool()
    async def follow_plan(plan_id: str, ctx: Context) -> dict[str, Any]:
        """Follow a plan_id returned by A* plan_route and wait for verified arrival."""
        if walk_lock.locked():
            raise ServiceError("the simulation MCP service is already handling a plan")
        async with walk_lock:
            if plan_id in submitted:
                raise ServiceError("plan was already submitted; query the walk status")
            if plan_id in failed_previews:
                raise ServiceError("plan preview failed; replan before walking")
            route, health = await load_plan(plan_id)
            if not simulator_can_move(health):
                raise ServiceError("simulator is unavailable or busy")
            legs = route.get("legs")
            if not isinstance(legs, list) or len(legs) != 1:
                raise ServiceError("follow_plan requires a single-goal plan")
            pose = await to_thread.run_sync(simulation.get_pose)
            start = route["start"]
            x = finite_number(start.get("x"), "start.x")
            y = finite_number(start.get("y"), "start.y")
            if pose["fallen"] or math.hypot(pose["x"] - x, pose["y"] - y) > 0.3:
                raise ServiceError(
                    "the simulator moved away from the plan start or fell"
                )
            walk_id = None
            finished = False
            try:
                with CancelScope(shield=True):
                    submitted.add(plan_id)
                    accepted = await to_thread.run_sync(simulation.start_walk, route)
                    walk_id = accepted["walk_id"]
                try:
                    with fail_after(walk_timeout_s):
                        request = ctx.request_context.request
                        status = await _wait_for_walk(
                            simulation,
                            walk_id,
                            request if isinstance(request, Request) else None,
                        )
                except TimeoutError as exc:
                    raise ServiceError(
                        f"walk {walk_id} did not finish within {walk_timeout_s:g} seconds"
                    ) from exc
                finished = True
                validate_walk_arrival(status, route)
            finally:
                if walk_id is not None and not finished:
                    await _stop_walk(simulation, walk_id)
            return {
                "ok": True,
                "navigation_completed": True,
                "plan_id": plan_id,
                "walk_id": walk_id,
                "state": "finished",
                "pose": status["pose"],
                "result": status["result"],
            }

    return server


def main() -> None:
    """Run the MuJoCo MCP endpoint and close its clients on exit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim-endpoint", default="http://127.0.0.1:8770")
    parser.add_argument("--astar-mcp-url", default="http://127.0.0.1:8790/mcp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8791, type=int)
    parser.add_argument("--walk-timeout-s", default=120.0, type=float)
    args = parser.parse_args()
    with ExitStack() as clients:
        simulation = SimulationClient(args.sim_endpoint, timeout_s=12)
        clients.callback(simulation.close)
        planner = McpClient("astar", McpServerConfig(url=args.astar_mcp_url))
        clients.callback(planner.close)
        create_mujoco_mcp(
            simulation,
            planner,
            host=args.host,
            port=args.port,
            walk_timeout_s=args.walk_timeout_s,
        ).run(transport="streamable-http")


if __name__ == "__main__":
    main()

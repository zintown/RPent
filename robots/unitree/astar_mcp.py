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

"""Expose the existing A* HTTP service through a Streamable HTTP MCP endpoint."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from robots.unitree.navigation_client import (
    CoordinateGoal,
    NavigationClient,
    NavigationGoal,
)


def create_astar_mcp(navigation: NavigationClient, *, host: str, port: int) -> FastMCP:
    """Build an MCP server backed by an existing navigation client."""
    server = FastMCP(
        "unitree-navigation",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    plans: dict[str, dict[str, Any]] = {}

    @server.tool(annotations=readonly)
    async def list_waypoints() -> dict[str, Any]:
        """List the map's named destinations and standing poses."""
        return await asyncio.to_thread(navigation.list_waypoints)

    @server.tool(annotations=readonly)
    async def plan_route(start: CoordinateGoal, goal: NavigationGoal) -> dict[str, Any]:
        """Plan to one target from the fresh simulator x, y and yaw and return a plan_id for follow_plan."""
        route = await asyncio.to_thread(
            navigation.plan, start.model_dump(exclude_none=True), [goal]
        )
        plan_id = f"plan_{uuid4().hex}"
        plans[plan_id] = route
        if len(plans) > 256:
            del plans[next(iter(plans))]
        return {
            "ok": True,
            "plan_id": plan_id,
            "map_id": route["map_id"],
            "map_version": route["map_version"],
            "goal": route["legs"][0]["goal"],
            "total_length": route["total_length"],
            "path_points": len(route["legs"][0]["path"]),
            "warnings": route.get("warnings", []),
        }

    @server.tool(annotations=readonly)
    def get_plan(plan_id: str) -> dict[str, Any]:
        """Return the exact route saved by plan_route for the simulation service."""
        if plan_id not in plans:
            raise ValueError("unknown plan_id; plan again from the current pose")
        return plans[plan_id]

    return server


def main() -> None:
    """Serve the configured A* planner over MCP."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nav-endpoint", default="http://127.0.0.1:8765")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    navigation = NavigationClient(args.nav_endpoint)
    try:
        server = create_astar_mcp(navigation, host=args.host, port=args.port)
        server.run(transport="streamable-http")
    finally:
        navigation.close()


if __name__ == "__main__":
    main()

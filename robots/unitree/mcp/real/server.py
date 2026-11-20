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

"""Expose physical Go2 communication through a task-scoped MCP server."""

from __future__ import annotations

import argparse
import os
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from anyio import to_thread
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from robots.unitree.mcp.real.actions import RealRobotActions, _summary
from robots.unitree.mcp.real.client import RealRobotClient


def create_real_mcp(
    client: RealRobotClient, *, output_dir: Path, action_timeout_s: float = 180.0
) -> FastMCP:
    """Expose calls bound to one configured robot index."""
    actions = RealRobotActions(
        real=client, state_output_dir=output_dir, action_timeout_s=action_timeout_s
    )
    server = FastMCP("unitree-go2")
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

    @server.tool(annotations=readonly)
    async def list_robots() -> dict[str, Any]:
        """List the online physical Go2 indices on this communication service."""
        return await to_thread.run_sync(client.list_robots)

    @server.tool(annotations=readonly)
    async def get_robot_status() -> dict[str, Any]:
        """Read connection, heartbeat and camera state for the assigned physical Go2."""
        return await to_thread.run_sync(client.health)

    @server.tool(annotations=readonly)
    async def get_robot_request(request_id: str) -> dict[str, Any]:
        """Read an earlier move, capture or stop request for this robot."""
        return _summary(await to_thread.run_sync(client.get_request, request_id))

    @server.tool()
    async def move_relative(
        dx_m: float, dy_m: float, arrival_turn_deg: float
    ) -> dict[str, Any]:
        """Move the physical Go2 using body-frame offsets of up to 5 m total.

        Args:
            dx_m: Forward (+) or rear (-) offset in meters.
            dy_m: Left (+) or right (-) offset in meters.
            arrival_turn_deg: Extra turn from the arrival heading, positive to the left.
        """
        return await to_thread.run_sync(
            actions.perform, actions.move_relative, dx_m, dy_m, arrival_turn_deg
        )

    @server.tool()
    async def capture_image(
        rgbd: bool = False, after_motion: bool = False
    ) -> dict[str, Any]:
        """Wait for the physical camera capture and return file paths and metadata."""
        capture = partial(actions.capture_image, rgbd=rgbd, after_motion=after_motion)
        return await to_thread.run_sync(actions.perform, capture)

    @server.tool()
    async def stop_robot() -> dict[str, Any]:
        """Request stop and queue clearing for this robot and verify its feedback."""
        return await to_thread.run_sync(actions.perform, actions.stop_robot)

    return server


def main() -> None:
    """Serve Go2 tools over stdio and close the owned communication client."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:9001")
    parser.add_argument("--robot-index", type=int, required=True)
    parser.add_argument("--action-timeout-s", type=float, default=180.0)
    parser.add_argument(
        "--output-dir", type=Path, default=os.environ.get("RPENT_TASK_OUTPUT_DIR")
    )
    args = parser.parse_args()
    if args.endpoint.rstrip("/").endswith("/call"):
        parser.error("--endpoint must be the communication service base URL")
    output = args.output_dir or Path("logs") / f"go2-mcp-{uuid4().hex[:12]}"
    client = RealRobotClient(args.endpoint, robot_index=args.robot_index)
    try:
        create_real_mcp(
            client, output_dir=output, action_timeout_s=args.action_timeout_s
        ).run(transport="stdio")
    finally:
        client.close()


if __name__ == "__main__":
    main()

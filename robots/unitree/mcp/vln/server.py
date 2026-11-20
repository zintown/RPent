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

"""Run a LightNav navigation episode through a task-scoped MCP server."""

from __future__ import annotations

import argparse
import math
import os
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from anyio import from_thread, get_cancelled_exc_class, to_thread
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from robots.unitree.http_client import ServiceError
from robots.unitree.mcp.vln.model_client import VlnClient
from robots.unitree.mcp.vln.navigation import VlnCancelled, VlnNavigation
from robots.unitree.mcp.vln.observation_client import ObservationClient
from robots.unitree.navigation_client import CoordinateGoal
from robots.unitree.simulation_client import SimulationClient


def create_vln_mcp(navigation: VlnNavigation, *, timeout_s: float = 120.0) -> FastMCP:
    """Expose the existing image, inference and waypoint loop as MCP tools."""
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("VLN budget must be finite and positive")
    server = FastMCP("unitree-vln")
    episode_budget = timeout_s

    @server.tool(
        description=(
            f"Run one visual navigation episode and wait for stopping. "
            f"Pass the current visual instruction. expected_goal is an optional known "
            f"map standing pose for verification. timeout_s defaults to {timeout_s:g} "
            f"seconds and cannot exceed that budget."
        )
    )
    async def run_vln_navigation(
        instruction: str,
        expected_goal: CoordinateGoal | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        budget = episode_budget if timeout_s is None else timeout_s
        if not 0 < budget <= episode_budget:
            raise ValueError(
                "VLN timeout must be positive and within the configured budget"
            )
        cancelled = get_cancelled_exc_class()

        def check_cancelled() -> None:
            try:
                from_thread.check_cancelled()
            except cancelled as exc:
                raise VlnCancelled("VLN navigation was cancelled") from exc

        run = partial(
            navigation.run,
            instruction,
            timeout_s=budget,
            expected_goal=None
            if expected_goal is None
            else expected_goal.model_dump(exclude_none=True),
            check_cancelled=check_cancelled,
        )
        return {"ok": True, **await to_thread.run_sync(run)}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def get_vln_status() -> dict[str, Any]:
        """Read the current or latest VLN episode and its stopping and goal evidence."""
        return navigation.snapshot()

    @server.tool()
    async def stop_vln_navigation() -> dict[str, Any]:
        """Cancel the owned VLN control lease and check simulator stop confirmation."""
        was_active = navigation.active
        await to_thread.run_sync(navigation.cancel)
        state = navigation.snapshot()
        if state["active_control_id"] is not None or state["submission_uncertain"]:
            raise ServiceError(
                state["stop_error"] or "VLN stopping requires reconciliation"
            )
        return {"ok": True, "stopped": was_active, **state}

    return server


def main() -> None:
    """Serve the VLN tools over stdio and release the backend connections on exit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim-endpoint", default="http://127.0.0.1:8770")
    parser.add_argument("--observation-endpoint", default="http://127.0.0.1:8771")
    parser.add_argument("--vln-endpoint", default="ws://127.0.0.1:8050")
    parser.add_argument("--vln-timeout-s", type=float, default=120.0)
    parser.add_argument("--vln-inference-timeout-s", type=float, default=3.0)
    parser.add_argument(
        "--output-dir", type=Path, default=os.environ.get("RPENT_TASK_OUTPUT_DIR")
    )
    args = parser.parse_args()
    output = args.output_dir or Path("logs") / f"vln-mcp-{uuid4().hex[:12]}"
    with ExitStack() as clients:
        sim = SimulationClient(args.sim_endpoint, timeout_s=5)
        clients.callback(sim.close)
        observation = ObservationClient(args.observation_endpoint, timeout_s=3)
        clients.callback(observation.close)
        model = VlnClient(args.vln_endpoint, timeout_s=args.vln_inference_timeout_s)
        clients.callback(model.close)
        navigation = VlnNavigation(
            sim=sim,
            observation=observation,
            model=model,
            artifacts=output / "navigation" / "vln",
        )
        create_vln_mcp(navigation, timeout_s=args.vln_timeout_s).run(transport="stdio")


if __name__ == "__main__":
    main()

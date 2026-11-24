# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

"""Expose robot discovery and natural-language task execution over MCP."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from anyio import CancelScope, get_cancelled_exc_class, to_thread
from anyio.lowlevel import checkpoint
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from rpent.mcp.task.config import TaskDeployment
from rpent.mcp.task.discovery import RobotInfo, RobotState, check_robot
from rpent.mcp.task.runner import TaskRunner


class TaskResult(BaseModel):
    """Terminal task result and available execution evidence."""

    robot_id: str
    status: str
    summary: str | None
    error: str | None
    output_dir: str | None
    evidence: dict[str, Any]
    navigation: dict[str, Any] | None


def _runner_state(runner: TaskRunner) -> RobotState:
    health = runner.health()
    if health["requires_attention"]:
        return "requires_attention"
    if health["busy"]:
        return "busy"
    return "idle" if health["ok"] else "unavailable"


def _robot_info(runner: TaskRunner, deployment: TaskDeployment) -> RobotInfo:
    info = check_robot(deployment, _runner_state(runner))
    # A task may have claimed or released the runner during downstream discovery.
    latest = _runner_state(runner)
    if latest != "idle" and latest != info.state:
        return check_robot(deployment, latest)
    return info


def create_task_mcp(
    get_runner: Callable[[], TaskRunner],
    *,
    deployment: TaskDeployment,
    allowed_hosts: list[str] | None = None,
) -> FastMCP:
    """Build a discovery and task MCP server for one configured robot.

    Args:
        get_runner: Accessor for the runner owned by the HTTP lifespan.
        deployment: Identity and services bound to this endpoint.
        allowed_hosts: Additional trusted Host headers for private-network access.

    Returns:
        A server whose session manager is owned by the enclosing application.
    """
    robot_id = deployment.robot.id
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *allowed_hosts],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        )
    server = FastMCP(
        "rpent-tasks",
        instructions=(
            f"This endpoint controls {robot_id}. Call get_robot_info to check its "
            "identity, environment and capabilities before dispatch. Choose an idle "
            "robot with the ready capabilities required by the instruction. "
            "Use run_task to execute a complete instruction. "
            "Preserve the requested action, order, and acceptance criteria. "
            "The tool returns the terminal status. Use its summary and execution "
            "evidence to report the outcome. Treat each call as a new execution. "
            "Check the robot after an uncertain outcome before submitting more motion."
        ),
        json_response=True,
        transport_security=security,
    )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_robot_info() -> RobotInfo:
        """Read identity, availability, and verified capabilities for robot selection."""
        return await to_thread.run_sync(_robot_info, get_runner(), deployment)

    @server.tool(
        description=(
            f"Execute one natural-language task on {robot_id} and return its final status. "
            "Pass the complete task text in instruction. Each call starts a new execution."
        )
    )
    async def run_task(
        instruction: Annotated[str, Field(min_length=1, max_length=20000)],
    ) -> TaskResult:
        runner = get_runner()
        await checkpoint()
        with CancelScope(shield=True):
            job = await to_thread.run_sync(runner.start, instruction)
        try:
            record = await to_thread.run_sync(runner.wait, job, abandon_on_cancel=True)
        except get_cancelled_exc_class():
            with CancelScope(shield=True):
                await to_thread.run_sync(runner.cancel, job)
            raise

        return TaskResult(
            robot_id=robot_id,
            status=record["status"],
            summary=(record["finish"] or {}).get("summary"),
            error=record["error"],
            output_dir=record["output_dir"],
            evidence=record["evidence"],
            navigation=record["navigation"],
        )

    return server

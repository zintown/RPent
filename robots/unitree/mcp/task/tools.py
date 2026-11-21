# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

"""Expose natural-language task execution as a discoverable MCP tool."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated, Any

from anyio import CancelScope, get_cancelled_exc_class, to_thread
from anyio.lowlevel import checkpoint
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from robots.unitree.mcp.task.runner import TaskRunner


class TaskResult(BaseModel):
    """Terminal task result and available execution evidence."""

    robot_id: str
    status: str
    summary: str | None
    error: str | None
    output_dir: str | None
    navigation: dict[str, Any] | None


def create_task_mcp(
    get_runner: Callable[[], TaskRunner],
    *,
    robot_id: str,
    allowed_hosts: list[str] | None = None,
) -> FastMCP:
    """Build an MCP server with one complete task call for its configured robot.

    Args:
        get_runner: Accessor for the runner owned by the HTTP lifespan.
        robot_id: Identity of the robot bound to this endpoint.
        allowed_hosts: Additional trusted Host headers for private-network access.

    Returns:
        A server whose session manager is owned by the enclosing application.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", robot_id):
        raise ValueError("robot_id must be 1–64 ASCII letters, digits, _, - or .")
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
            f"Use run_task to execute a complete instruction on {robot_id}. "
            "Preserve the requested action, order, and acceptance criteria. "
            "The tool returns the terminal status. Use its summary and execution "
            "evidence to report the outcome. Treat each call as a new execution. "
            "Check the robot after an uncertain outcome before submitting more motion."
        ),
        json_response=True,
        transport_security=security,
    )

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
            navigation=record["navigation"],
        )

    return server

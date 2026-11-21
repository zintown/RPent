# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Check configured capability prerequisites against live MCP services."""

from __future__ import annotations

import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory
from typing import Any, Literal

from mcp import types
from pydantic import BaseModel, Field

from rpent.mcp.task.config import (
    CapabilityConfig,
    CheckValue,
    Environment,
    TaskDeployment,
)
from rpent.tools.mcp_client import McpClient, McpServerConfig, McpServiceError

RobotState = Literal["idle", "busy", "requires_attention", "unavailable"]


class CapabilityInfo(BaseModel):
    """A declared capability and the current result of checking its prerequisites."""

    name: str
    description: str
    status: Literal["ready", "unavailable", "unverified"]
    reason: str | None = None


class RobotInfo(BaseModel):
    """Information that a dispatcher can use to select an available robot."""

    robot_id: str
    robot_type: str
    environment: Environment | None
    state: RobotState
    capabilities: list[CapabilityInfo]
    service_errors: dict[str, str] = Field(default_factory=dict)


@dataclass
class _Discovery:
    tools: set[str] = field(default_factory=set)
    checks: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    error: str | None = None


def _check_result(client: McpClient, tool: str) -> dict[str, Any] | None:
    definition = client.tools.get(tool)
    if definition is None or (
        definition.annotations is None
        or definition.annotations.readOnlyHint is not True
    ):
        return None
    try:
        result = client.call_tool(tool, {})
        if result.isError:
            return None
        if result.structuredContent is not None:
            return result.structuredContent
        if len(result.content) == 1 and isinstance(
            result.content[0], types.TextContent
        ):
            data = json.loads(result.content[0].text)
            return data if isinstance(data, dict) else None
    except (McpServiceError, ValueError):
        pass
    return None


def _discover(
    name: str, config: McpServerConfig, checks: set[str], output_dir: str
) -> _Discovery:
    bounded = config.model_copy(
        update={
            "startup_timeout_sec": min(config.startup_timeout_sec, 5),
            "tool_timeout_sec": min(config.tool_timeout_sec, 8),
        }
    )
    try:
        with closing(
            McpClient(
                name, bounded, env_overrides={"RPENT_TASK_OUTPUT_DIR": output_dir}
            )
        ) as client:
            if config.run_report_tool is not None:
                try:
                    client.require_readonly_tool(config.run_report_tool)
                except McpServiceError as exc:
                    return _Discovery(error=str(exc))
            selected = set(
                client.tools if config.enabled_tools is None else config.enabled_tools
            )
            missing = selected - client.tools.keys()
            if missing:
                return _Discovery(
                    error=f"configured tools were not exported: {', '.join(sorted(missing))}"
                )
            return _Discovery(
                selected, {tool: _check_result(client, tool) for tool in checks}
            )
    except McpServiceError:
        return _Discovery(error="MCP server could not be reached or initialized")


def _matches(data: dict[str, Any], key: str, expected: CheckValue) -> bool:
    value: Any = data
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return type(value) is type(expected) and value == expected


def _capability_problem(
    config: CapabilityConfig, discovered: dict[str, _Discovery]
) -> str | None:
    for reference in config.tools:
        server, tool = reference.split(".", 1)
        found = discovered.get(server)
        if found is None or tool not in found.tools:
            return f"required tool {reference} is unavailable"
    for reference, expected in config.checks.items():
        server, tool = reference.split(".", 1)
        found = discovered.get(server)
        data = found.checks.get(tool) if found else None
        if data is None:
            return f"read-only check {reference} could not be verified"
        if data.get("ok") is False or not all(
            _matches(data, key, value) for key, value in expected.items()
        ):
            return f"readiness check {reference} failed"
    return None


def check_robot(deployment: TaskDeployment, state: RobotState) -> RobotInfo:
    """Return live capability status when the runner can accept a new task."""
    discovered: dict[str, _Discovery] = {}
    if state == "idle":
        active = {
            name: config
            for name, config in deployment.servers.items()
            if config.enabled and config.enabled_tools != []
        }
        if active:
            checks: dict[str, set[str]] = defaultdict(set)
            for capability in deployment.capabilities.values():
                for reference in capability.checks:
                    server, tool = reference.split(".", 1)
                    checks[server].add(tool)
            with TemporaryDirectory(prefix="rpent-mcp-probe-") as output_dir:
                with ThreadPoolExecutor(max_workers=min(len(active), 8)) as pool:
                    pending = {
                        name: pool.submit(
                            _discover, name, config, checks[name], output_dir
                        )
                        for name, config in active.items()
                    }
                    discovered = {
                        name: future.result() for name, future in pending.items()
                    }
        if not any(result.tools for result in discovered.values()) or any(
            result.error for result in discovered.values()
        ):
            state = "unavailable"

    capabilities = []
    for name, config in deployment.capabilities.items():
        if not discovered and state in {"busy", "requires_attention"}:
            status, reason = "unverified", "runner cannot accept a task now"
        elif problem := _capability_problem(config, discovered):
            status, reason = "unavailable", problem
        elif not config.checks:
            status, reason = "unverified", "no live checks configured"
        else:
            status, reason = "ready", None
        capabilities.append(
            CapabilityInfo(
                name=name, description=config.description, status=status, reason=reason
            )
        )
    return RobotInfo(
        robot_id=deployment.robot.id,
        robot_type=deployment.robot.type,
        environment=deployment.robot.environment,
        state=state,
        capabilities=capabilities,
        service_errors={
            name: result.error for name, result in discovered.items() if result.error
        },
    )

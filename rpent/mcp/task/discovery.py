# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Check configured capability prerequisites against live MCP services."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory
from typing import Any, Literal

from pydantic import BaseModel, Field

from rpent.mcp.task.config import (
    CapabilityConfig,
    Environment,
    TaskDeployment,
)
from rpent.tools.mcp_client import (
    McpClient,
    McpServerConfig,
    McpServiceError,
    check_matches,
)

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
    environment: Environment
    state: RobotState
    capabilities: list[CapabilityInfo]
    service_errors: dict[str, str] = Field(default_factory=dict)


@dataclass
class _Discovery:
    tools: set[str] = field(default_factory=set)
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None


def _check_result(client: McpClient, tool: str) -> dict[str, Any]:
    try:
        return client.read_check(tool)
    except McpServiceError as exc:
        return {"ok": False, "reason": str(exc)}


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
            try:
                selected = set(client.select_tools(config))
            except McpServiceError as exc:
                return _Discovery(error=str(exc))
            return _Discovery(
                selected, {tool: _check_result(client, tool) for tool in checks}
            )
    except McpServiceError:
        return _Discovery(error="MCP server could not be reached or initialized")


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
            check_matches(data, key, value) for key, value in expected.items()
        ):
            reason = data.get("reason")
            detail = f" ({reason})" if isinstance(reason, str) and reason else ""
            return f"readiness check {reference} failed{detail}"
    return None


def check_robot(deployment: TaskDeployment, state: RobotState) -> RobotInfo:
    """Return live capability status when the runner can accept a new task."""
    discovered: dict[str, _Discovery] = {}
    workflow_checks: dict[str, str | None] = {}
    if state == "idle":
        workflows = {
            name: cap.bindings
            for name, cap in deployment.capabilities.items()
            if cap.bindings
        }
        bound_services = {
            service for bindings in workflows.values() for service in bindings.values()
        }
        direct_services = {
            ref.split(".", 1)[0]
            for cap in deployment.capabilities.values()
            for ref in (*cap.tools, *cap.checks)
        }
        active = {
            name: config
            for name, config in deployment.servers.items()
            if config.enabled
            and config.enabled_tools != []
            and (name not in bound_services or name in direct_services)
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
        check_capability = deployment.execution.check_capability
        if check_capability is not None:
            workflow_checks = {name: check_capability(name) for name in workflows}
        if any(result.error for result in discovered.values()) or (
            not workflows and not any(result.tools for result in discovered.values())
        ):
            state = "unavailable"

    capabilities = []
    for name, config in deployment.capabilities.items():
        if not discovered and state in {"busy", "requires_attention"}:
            status, reason = "unverified", "runner cannot accept a task now"
        elif config.bindings:
            reason = workflow_checks.get(
                name, "workflow readiness could not be verified"
            )
            status = "unavailable" if reason is not None else "ready"
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

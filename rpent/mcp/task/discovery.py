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
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from pydantic import BaseModel, Field

from rpent.mcp.task.config import (
    CapabilityConfig,
    CheckField,
    Environment,
    TaskDeployment,
)
from rpent.tools.mcp_client import McpServiceError, check_matches
from rpent.tools.mcp_registry import McpService, McpServiceRegistry

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


def _check_result(service: McpService, tool: str) -> dict[str, Any]:
    try:
        return service.read_check(tool)
    except McpServiceError as exc:
        return {"ok": False, "reason": str(exc)}


def _discover(registry: McpServiceRegistry, name: str, checks: set[str]) -> _Discovery:
    try:
        service = registry.connect(name)
    except McpServiceError:
        return _Discovery()
    return _Discovery(
        set(service.model_tools),
        {tool: _check_result(service, tool) for tool in checks},
    )


def _probe(
    deployment: TaskDeployment,
) -> tuple[dict[str, _Discovery], dict[str, str | None], dict[str, str]]:
    checks: dict[str, set[str]] = defaultdict(set)
    for capability in deployment.capabilities.values():
        references = set(capability.checks)
        references.update(
            value.tool
            for fields in capability.checks.values()
            for value in fields.values()
            if isinstance(value, CheckField)
        )
        for reference in references:
            server, tool = reference.split(".", 1)
            checks[server].add(tool)
    direct = [
        name
        for name, config in deployment.servers.items()
        if config.enabled
        and config.enabled_tools != []
        and (config.model_tools != [] or name in checks)
    ]
    discovered: dict[str, _Discovery] = {}
    workflow_checks: dict[str, str | None] = {}
    with TemporaryDirectory(prefix="rpent-mcp-probe-") as output:
        with closing(
            McpServiceRegistry(deployment.servers, probe_output=Path(output))
        ) as registry:
            if direct:
                with ThreadPoolExecutor(max_workers=min(len(direct), 8)) as pool:
                    pending = {
                        name: pool.submit(_discover, registry, name, checks[name])
                        for name in direct
                    }
                    discovered = {
                        name: future.result() for name, future in pending.items()
                    }
            for name, check in deployment.execution.capability_checks.items():
                if (
                    name not in deployment.capabilities
                    or not deployment.capabilities[name].bindings
                ):
                    continue
                try:
                    workflow_checks[name] = check(registry)
                except McpServiceError as exc:
                    workflow_checks[name] = str(exc)
            return discovered, workflow_checks, registry.errors


def _check_data(
    discovered: dict[str, _Discovery], reference: str
) -> dict[str, Any] | None:
    server, tool = reference.split(".", 1)
    found = discovered.get(server)
    return found.checks.get(tool) if found else None


def _capability_problem(
    config: CapabilityConfig, discovered: dict[str, _Discovery]
) -> str | None:
    for reference in config.tools:
        server, tool = reference.split(".", 1)
        found = discovered.get(server)
        if found is None or tool not in found.tools:
            return f"required tool {reference} is unavailable"
    for reference, expected in config.checks.items():
        data = _check_data(discovered, reference)
        if data is None:
            return f"read-only check {reference} could not be verified"
        reason = data.get("reason")
        detail = f" ({reason})" if isinstance(reason, str) else ""
        failed = f"readiness check {reference} failed{detail}"
        if data.get("ok") is False:
            return failed
        for key, value in expected.items():
            if isinstance(value, CheckField):
                other = _check_data(discovered, value.tool)
                if other is None or other.get("ok") is False:
                    return f"read-only check {value.tool} could not be verified"
                field: Any = other
                for part in value.field.split("."):
                    field = field.get(part) if isinstance(field, dict) else None
                if not isinstance(field, (bool, int, float, str)):
                    return f"read-only check {value.tool} did not report {value.field}"
                if not check_matches(data, key, field):
                    return f"readiness field {reference}.{key} differs from {value.tool}.{value.field}"
            elif not check_matches(data, key, value):
                return failed
    return None


def check_robot(deployment: TaskDeployment, state: RobotState) -> RobotInfo:
    """Return live capability status when the runner can accept a new task."""
    discovered: dict[str, _Discovery] = {}
    workflow_checks: dict[str, str | None] = {}
    service_errors: dict[str, str] = {}
    if state == "idle":
        discovered, workflow_checks, service_errors = _probe(deployment)
        if not workflow_checks and not any(
            result.tools for result in discovered.values()
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
        service_errors=service_errors,
    )

# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Read a robot deployment and its capability contracts from MCP configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from rpent.robots.base import get_robot_spec
from rpent.robots.robot_spec import TaskExecutionSpec
from rpent.tools.mcp_client import (
    McpCheckValue,
    McpServerConfig,
    parse_mcp_servers,
    read_mcp_config,
)

RobotName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
Environment = Literal["simulation", "real"]
ToolReference = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+\.\S+$")]
ServerName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$")]
AdapterName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class RobotIdentity(BaseModel):
    """Identity and RPent adapter of one robot deployment."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: RobotName
    type: RobotName
    environment: Environment
    adapter: AdapterName = "mcp"


class CheckField(BaseModel):
    """Compare with a scalar field from a second read-only check."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool: ToolReference
    field: Annotated[str, Field(min_length=1)]


class CapabilityConfig(BaseModel):
    """Declare a direct tool capability or bind providers to a workflow."""

    model_config = ConfigDict(extra="forbid", strict=True)

    description: Annotated[str, Field(min_length=1)]
    tools: list[ToolReference] = Field(default_factory=list)
    checks: dict[
        ToolReference,
        Annotated[dict[str, McpCheckValue | CheckField], Field(min_length=1)],
    ] = Field(default_factory=dict)
    bindings: dict[AdapterName, ServerName] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_capability(self) -> CapabilityConfig:
        if bool(self.tools) == bool(self.bindings) or (self.bindings and self.checks):
            raise ValueError(
                "configure either tools with optional checks or workflow bindings"
            )
        return self


class _CapabilityTable(BaseModel):
    """Validate capability identifiers and definitions."""

    entries: dict[RobotName, CapabilityConfig]


class ReadOnlyQueryConfig(BaseModel):
    """Expose configured no-argument observations to the dispatcher."""

    model_config = ConfigDict(extra="forbid", strict=True)

    description: Annotated[str, Field(min_length=1)]
    tools: Annotated[list[ToolReference], Field(min_length=1)]


@dataclass(frozen=True)
class TaskDeployment:
    """Configuration shared by discovery and every task launched by the service."""

    robot: RobotIdentity
    servers: dict[str, McpServerConfig]
    capabilities: dict[str, CapabilityConfig]
    execution: TaskExecutionSpec
    service_adapters: dict[str, str] = field(default_factory=dict)
    read_only_queries: dict[str, ReadOnlyQueryConfig] = field(default_factory=dict)


def load_deployment(
    path: Path,
    *,
    robot_id: str | None = None,
    environment: Environment | None = None,
) -> TaskDeployment:
    """Load one deployment and resolve its natural-language execution adapter."""
    path = path.expanduser().resolve()
    config = read_mcp_config(path)
    identity = config.get("robot")
    if not isinstance(identity, dict):
        raise ValueError(f"{path} must contain a [robot] table")
    identity = dict(identity)
    if robot_id is not None:
        identity["id"] = robot_id
    if environment is not None:
        identity["environment"] = environment
    robot = RobotIdentity.model_validate(identity)
    execution = get_robot_spec(robot.adapter).task_execution
    if execution is None:
        raise ValueError(f"robot adapter {robot.adapter!r} does not support task MCP")

    servers = parse_mcp_servers(config, path)
    capabilities = _CapabilityTable(entries=config.get("capabilities", {})).entries
    read_only_queries = TypeAdapter(
        dict[ServerName, ReadOnlyQueryConfig]
    ).validate_python(config.get("read_only_queries", {}), strict=True)
    if conflicts := read_only_queries.keys() & {"get_robot_info", "run_task"}:
        raise ValueError(f"read-only query names are reserved: {sorted(conflicts)}")
    unknown_query_servers = {
        reference.split(".", 1)[0]
        for query in read_only_queries.values()
        for reference in query.tools
    } - servers.keys()
    if unknown_query_servers:
        raise ValueError(
            f"read-only queries reference unknown services: {sorted(unknown_query_servers)}"
        )
    service_adapters = TypeAdapter(dict[ServerName, AdapterName]).validate_python(
        config.get("service_adapters", {}), strict=True
    )
    unknown_servers = service_adapters.keys() - servers.keys()
    if unknown_servers:
        raise ValueError(
            f"service_adapters references unknown services: {sorted(unknown_servers)}"
        )
    bound_services = {
        service for cap in capabilities.values() for service in cap.bindings.values()
    }
    unknown_bindings = bound_services - servers.keys()
    if unknown_bindings:
        raise ValueError(
            f"workflow bindings reference unknown services: {sorted(unknown_bindings)}"
        )
    for name in bound_services:
        if servers[name].model_tools is None:
            servers[name] = servers[name].model_copy(update={"model_tools": []})
    deployment = TaskDeployment(
        robot, servers, capabilities, execution, service_adapters, read_only_queries
    )
    if execution.configure_deployment is not None:
        deployment = execution.configure_deployment(deployment)
    missing_checks = {
        name for name, cap in deployment.capabilities.items() if cap.bindings
    } - deployment.execution.capability_checks.keys()
    if missing_checks:
        raise ValueError(
            f"robot adapter {robot.adapter!r} has no workflow checks for {sorted(missing_checks)}"
        )
    return deployment

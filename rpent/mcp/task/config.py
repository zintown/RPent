# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Read a robot deployment and its capability contracts from MCP configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from rpent.robots.base import get_robot_spec
from rpent.robots.robot_spec import TaskExecutionSpec
from rpent.tools.mcp_client import McpServerConfig, parse_mcp_servers

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

RobotName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
Environment = Literal["simulation", "real"]
ToolReference = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+\.\S+$")]
CheckValue = bool | int | float | str


class RobotIdentity(BaseModel):
    """Identity and RPent adapter of one robot deployment."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: RobotName
    type: RobotName
    environment: Environment
    adapter: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")] = "mcp"


class CapabilityConfig(BaseModel):
    """Tools required for a capability and read-only checks of live readiness."""

    model_config = ConfigDict(extra="forbid", strict=True)

    description: Annotated[str, Field(min_length=1)]
    tools: list[ToolReference] = Field(min_length=1)
    checks: dict[
        ToolReference, Annotated[dict[str, CheckValue], Field(min_length=1)]
    ] = Field(default_factory=dict)


class _CapabilityTable(BaseModel):
    """Validate capability identifiers and definitions."""

    entries: dict[RobotName, CapabilityConfig]


@dataclass(frozen=True)
class TaskDeployment:
    """Configuration shared by discovery and every task launched by the service."""

    robot: RobotIdentity
    servers: dict[str, McpServerConfig]
    capabilities: dict[str, CapabilityConfig]
    execution: TaskExecutionSpec


def load_deployment(
    path: Path,
    *,
    robot_id: str | None = None,
    environment: Environment | None = None,
) -> TaskDeployment:
    """Load one deployment and resolve its natural-language execution adapter."""
    path = path.expanduser().resolve()
    with path.open("rb") as handle:
        config = tomllib.load(handle)
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
    return TaskDeployment(robot, servers, capabilities, execution)

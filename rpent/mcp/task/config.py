# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Read a robot deployment and its capability contracts from MCP configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rpent.robots.base import get_robot_spec
from rpent.robots.robot_spec import TaskExecutionSpec
from rpent.tools.mcp_client import McpServerConfig, parse_mcp_servers, read_mcp_config

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
    return _parse_deployment(read_mcp_config(path), path, robot_id, environment)


def load_run_mcp_servers(path: Path) -> dict[str, McpServerConfig]:
    """Apply a physical deployment's tool policy to CLI runs using its TOML."""
    path = path.expanduser().resolve()
    config = read_mcp_config(path)
    identity = config.get("robot")
    if isinstance(identity, dict) and identity.get("environment") == "real":
        return _parse_deployment(config, path).servers
    return parse_mcp_servers(config, path)


def _parse_deployment(
    config: dict[str, Any],
    path: Path,
    robot_id: str | None = None,
    environment: Environment | None = None,
) -> TaskDeployment:
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
    deployment = TaskDeployment(robot, servers, capabilities, execution)
    if execution.configure_deployment is not None:
        return execution.configure_deployment(deployment)
    return deployment

# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Register Unitree's prompts and task evidence."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from robots.unitree.prompt_bundle import system_prompt, user_prompt
from robots.unitree.task_evidence import read_evidence
from rpent.robots.mcp_runtime import get_toolkit as get_toolkit
from rpent.robots.mcp_runtime import mcp_robot_spec
from rpent.robots.prompt_bundle import PromptBundle
from rpent.robots.robot_spec import RobotSpec, TaskExecutionSpec
from rpent.tools.mcp_client import mcp_tool_name

if TYPE_CHECKING:
    from rpent.mcp.task.config import TaskDeployment


def _tool_servers(deployment: TaskDeployment, tool: str) -> set[str]:
    return {
        name
        for name, config in deployment.servers.items()
        if config.enabled
        and (config.enabled_tools is None or tool in config.enabled_tools)
    }


def _configure_deployment(deployment: TaskDeployment) -> TaskDeployment:
    pose_servers = _tool_servers(deployment, "get_pose")
    declared_pose_servers = {
        reference.split(".", 1)[0]
        for name, capability in deployment.capabilities.items()
        if name in {"localization", "map_navigation"}
        for reference in capability.tools
        if reference.split(".", 1)[1] == "get_pose"
    }
    if declared_pose_servers:
        pose_servers &= declared_pose_servers

    follow_servers = _tool_servers(deployment, "follow_plan")
    execution = replace(
        deployment.execution,
        read_evidence=partial(
            read_evidence,
            pose_tools=frozenset(
                mcp_tool_name(name, "get_pose") for name in pose_servers
            ),
            follow_tools=frozenset(
                mcp_tool_name(name, "follow_plan") for name in follow_servers
            ),
        ),
    )
    return replace(deployment, execution=execution)


def get_robot_spec() -> RobotSpec:
    """Register the terminal task entry point for Unitree."""
    return mcp_robot_spec(
        "unitree",
        PromptBundle(system=system_prompt, user=user_prompt),
        TaskExecutionSpec(configure_deployment=_configure_deployment),
    )

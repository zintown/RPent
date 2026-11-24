# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Robot adapter for deployments whose local capabilities are MCP tools."""

from collections.abc import Mapping

from rpent.prompt.utils import PromptNode
from rpent.robots.mcp_runtime import get_toolkit as get_toolkit
from rpent.robots.mcp_runtime import mcp_robot_spec
from rpent.robots.prompt_bundle import PromptBundle
from rpent.robots.robot_spec import RobotSpec, TaskExecutionSpec


def _system(variables: Mapping[str, object] | None = None) -> PromptNode:
    return """Operate the configured robot using its discovered MCP tools.
Check fresh state and tool requirements. Resolve the full ordered list of requested
destinations using the available read-only catalog or planning tools before moving.
Keep each requested destination. If one cannot be resolved, report that destination
and end the task. Use only aliases or IDs confirmed by the service.
Use mcp_call_with_refs to pass structured results between tools. Omit path for the
whole result. Read the action outcome when RPent provides it. Otherwise use the
service's verification tools. Resolve uncertain motion before another action.
End with finish in the user's language. Use success when evidence confirms the
requested outcome and include any requested observations."""


def _user(variables: Mapping[str, object] | None = None) -> PromptNode:
    return {"TASK": "{{task}}"}


def get_robot_spec() -> RobotSpec:
    """Register the configuration-only MCP robot adapter."""
    return mcp_robot_spec(
        "mcp", PromptBundle(system=_system, user=_user), TaskExecutionSpec()
    )

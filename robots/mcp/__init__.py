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
Check fresh state and tool requirements before acting. Execute dependent steps in order
and verify each action using the tool's result or an independent status query. If a
capability is missing, a target is ambiguous, or a motion outcome is uncertain, stop
the task and report what remains. Resolve uncertain motion using status tools before
any further action. Every task ends with finish. Report success only when the evidence
confirms the requested outcome, and include the requested observations in the summary."""


def _user(variables: Mapping[str, object] | None = None) -> PromptNode:
    return {"TASK": "{{task}}"}


def get_robot_spec() -> RobotSpec:
    """Register the configuration-only MCP robot adapter."""
    return mcp_robot_spec(
        "mcp", PromptBundle(system=_system, user=_user), TaskExecutionSpec()
    )

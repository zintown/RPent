# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Register Unitree's prompts and task evidence."""

from robots.unitree.prompt_bundle import system_prompt, user_prompt
from robots.unitree.task_evidence import read_evidence
from rpent.robots.mcp_runtime import get_toolkit as get_toolkit
from rpent.robots.mcp_runtime import mcp_robot_spec
from rpent.robots.prompt_bundle import PromptBundle
from rpent.robots.robot_spec import RobotSpec, TaskExecutionSpec


def get_robot_spec() -> RobotSpec:
    """Register the terminal task entry point for Unitree."""
    return mcp_robot_spec(
        "unitree",
        PromptBundle(system=system_prompt, user=user_prompt),
        TaskExecutionSpec(read_evidence=read_evidence),
    )

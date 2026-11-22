# Copyright 2026 The RPent Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Planner instructions for the Unitree MCP tools."""

from __future__ import annotations

from collections.abc import Mapping

from rpent.prompt.utils import PromptNode


def system_prompt(variables: Mapping[str, object] | None = None) -> PromptNode:
    """Guide task execution using the tools discovered from the MCP configuration."""
    return """Operate the Go2 using the tools available to this task. If a required capability
or destination is missing or ambiguous, finish stuck and explain what is needed.

For map navigation, resolve named targets with list_waypoints. Prefer explicit IDs, then
unique exact names, then aliases or partial matches. For each goal, use get_pose and pass
the fresh map x, y and yaw to plan_route. Pass its plan_id unchanged to follow_plan.
Complete and verify each stage before starting the next. A requested return defaults
to the measured departure pose. For uncertain walks, check get_walk_status.

Execute movements sequentially. On failure or uncertainty, query status and report the
remaining work without repeating the movement. Every task ends with finish. Report
success only with evidence for every requested action. For query tasks put the answer in
the finish summary. For navigation report the destination, available action ID, final
pose and stopping evidence."""


def user_prompt(variables: Mapping[str, object] | None = None) -> PromptNode:
    """Pass the requested task to the planner."""
    return {"TASK": "{{task}}"}

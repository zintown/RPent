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

"""Check the Unitree planner receives one MCP-oriented prompt."""

from robots.unitree.robot_spec import get_robot_spec


def test_prompt_guides_map_navigation_tools():
    prompts = get_robot_spec().prompts
    system = prompts.render("system", variables={})
    for name in (
        "list_waypoints",
        "get_pose",
        "plan_route",
        "follow_plan",
        "get_walk_status",
    ):
        assert name in system
    user = prompts.render("user", variables={"task": "前往客厅"})
    assert "前往客厅" in user

# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Collect compact Unitree evidence from completed MCP calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rpent.robots.robot_spec import TaskEvidence


def read_evidence(_output: Path, transcript: dict[str, Any]) -> TaskEvidence:
    """Read compact evidence from tool results recorded by the task process."""
    navigation: dict[str, Any] = {}
    for message in transcript.get("messages", []):
        name = message.get("name")
        if message.get("role") != "tool" or name not in {
            "mcp__mujoco__get_pose",
            "mcp__mujoco__follow_plan",
        }:
            continue
        try:
            data = json.loads(message["content"])
        except (TypeError, ValueError):
            continue
        result = data.get("structured_content") if isinstance(data, dict) else None
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        if name == "mcp__mujoco__get_pose":
            navigation["pose"] = result
        elif result.get("navigation_completed") is True:
            navigation.update(
                pose=result["pose"],
                walk_id=result["walk_id"],
                completed_navigation_goals=navigation.get(
                    "completed_navigation_goals", 0
                )
                + 1,
            )
    return TaskEvidence(data={"navigation": navigation} if navigation else {})

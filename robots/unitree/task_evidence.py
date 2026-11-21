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


def read_evidence(output: Path, transcript: dict[str, Any]) -> TaskEvidence:
    """Read compact evidence from tool results recorded by the task process."""
    navigation: dict[str, Any] = {}
    for message in transcript.get("messages", []):
        name = message.get("name")
        if message.get("role") != "tool" or name not in {
            "mcp__mujoco__get_pose",
            "mcp__mujoco__follow_plan",
            "mcp__vln__run_vln_navigation",
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
        elif name == "mcp__vln__run_vln_navigation":
            navigation.update(
                vln=result, pose=result.get("pose") or navigation.get("pose")
            )
        elif result.get("navigation_completed") is True:
            navigation.update(
                pose=result["pose"],
                walk_id=result["walk_id"],
                completed_navigation_goals=navigation.get(
                    "completed_navigation_goals", 0
                )
                + 1,
            )
    vln = navigation.get("vln", {})
    stop_error = vln.get("stop_error")
    return TaskEvidence(
        data={"navigation": navigation} if navigation else {},
        requires_attention=bool(
            vln.get("active_control_id")
            or vln.get("submission_uncertain")
            or stop_error
        ),
        verification_required=(
            vln.get("state") == "verification_required" and not vln.get("error")
        ),
        error=(
            vln.get("error")
            or ("VLN navigation failed" if vln.get("state") == "failed" else None)
        ),
        stop_error=stop_error,
    )

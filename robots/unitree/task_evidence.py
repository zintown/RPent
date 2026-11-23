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


def read_evidence(
    _output: Path,
    transcript: dict[str, Any],
    *,
    pose_tools: frozenset[str],
    follow_tools: frozenset[str],
    use_run_report: bool,
) -> TaskEvidence:
    """Read compact evidence from tool results recorded by the task process."""
    navigation: dict[str, Any] = {}
    error = None
    tools = pose_tools | follow_tools
    for message in transcript.get("messages", []):
        name = message.get("name")
        if message.get("role") != "tool" or name not in tools:
            continue
        try:
            data = json.loads(message.get("content", ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if use_run_report and name in follow_tools:
            if data.get("motion_rejected") is True:
                detail = data.get("error")
                error = (
                    detail
                    if isinstance(detail, str)
                    else "Go2 rejected navigation before execution"
                )
            continue
        result = data.get("structured_content")
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        if name in pose_tools:
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
    return TaskEvidence(
        data={"navigation": navigation} if navigation else {}, error=error
    )

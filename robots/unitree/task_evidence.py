# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Interpret Unitree task artifacts and motion completion evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rpent.robots.robot_spec import TaskEvidence


def _read(path: Path) -> tuple[dict[str, Any], int]:
    try:
        modified = path.stat().st_mtime_ns
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, 0
    if not isinstance(data, dict):
        raise ValueError(f"expected an object in {path}")
    return data, modified


def _map_evidence(transcript: dict[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for message in transcript.get("messages", []):
        if message.get("role") != "tool" or message.get("name") not in {
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
        if message["name"] == "mcp__mujoco__get_pose":
            evidence["pose"] = result
        elif result.get("navigation_completed") is True:
            evidence.update(
                pose=result["pose"],
                walk_id=result["walk_id"],
                completed_navigation_goals=evidence.get("completed_navigation_goals", 0)
                + 1,
            )
    return evidence


def read_evidence(output: Path, transcript: dict[str, Any]) -> TaskEvidence:
    """Collect route and VLN evidence from the finished run."""
    navigation = _map_evidence(transcript)
    state, state_time = _read(output / "states.json")
    steps = state.get("steps", [])
    if steps:
        navigation.update(steps[-1].get("state") or {})
    else:
        state_time = 0
    # A cancelled tool can leave a report newer than the completed CLI step.
    reports = (output / "navigation" / "walks").glob("*.json")
    latest = max(reports, key=lambda path: path.stat().st_mtime_ns, default=None)
    if latest is not None:
        report, modified = _read(latest)
        if modified > state_time:
            navigation.update(
                (key, report[key])
                for key in (
                    "completed_navigation_goals",
                    "active_walk_id",
                    "submission_uncertain",
                    "pose",
                    "error",
                    "stop_error",
                )
                if key in report
            )
    vln, modified = _read(output / "navigation" / "vln" / "latest.json")
    if modified > state_time:
        navigation.update(
            vln=vln,
            active_control_id=vln.get("active_control_id"),
            submission_uncertain=vln.get("submission_uncertain", False),
            stop_error=vln.get("stop_error"),
            pose=vln.get("pose") or navigation.get("pose"),
        )
    stop_error = navigation.get("stop_error")
    return TaskEvidence(
        data={"navigation": navigation} if navigation else {},
        requires_attention=bool(
            navigation.get("active_walk_id")
            or navigation.get("active_control_id")
            or navigation.get("submission_uncertain")
            or stop_error
        ),
        verification_required=(
            vln.get("state") == "verification_required"
            and not navigation.get("error")
            and not vln.get("error")
        ),
        error=(
            vln.get("error")
            or ("VLN navigation failed" if vln.get("state") == "failed" else None)
        ),
        stop_error=stop_error,
    )

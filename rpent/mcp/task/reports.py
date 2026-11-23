# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Collect task-local MCP evidence after a child finishes or is interrupted."""

from __future__ import annotations

import time
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from rpent.robots.robot_spec import TaskEvidence
from rpent.tools.mcp_client import (
    McpClient,
    McpServerConfig,
    McpServiceError,
    check_matches,
)


class _RunReport(BaseModel):
    model_config = {"strict": True}

    state: Literal["running", "finished", "missing"]
    evidence: dict[str, dict[str, Any]] = Field(default_factory=dict)
    requires_attention: bool = False
    verification_required: bool = False
    error: str | None = None
    stop_error: str | None = None


def _read_report(
    name: str, config: McpServerConfig, run_id: str, timeout_s: float
) -> _RunReport:
    bounded = config.model_copy(
        update={"startup_timeout_sec": 4, "tool_timeout_sec": 4}
    )
    deadline = time.monotonic() + timeout_s
    with closing(McpClient(name, bounded, run_id=run_id)) as client:
        tool = config.run_report_tool
        assert tool is not None
        client.require_readonly_tool(tool)
        while True:
            result = client.call_tool(tool, {})
            if result.isError or result.structuredContent is None:
                raise McpServiceError(
                    f"{name}.{tool} did not return structured evidence"
                )
            report = _RunReport.model_validate(result.structuredContent)
            if report.state != "running" or time.monotonic() >= deadline:
                return report
            time.sleep(0.2)


def collect_run_evidence(
    servers: dict[str, McpServerConfig],
    output: Path,
    run_id: str,
    *,
    timeout_s: float,
    initial: TaskEvidence,
) -> TaskEvidence:
    """Read an owned report only for services that received an action attempt."""
    data = {**initial.data}
    attention = initial.requires_attention
    verification = initial.verification_required
    error, stop_error = initial.error, initial.stop_error
    markers = sorted(
        (output / ".mcp-actions").glob("*"),
        key=lambda marker: marker.stat().st_mtime_ns,
    )
    attempts: dict[str, bool] = {}
    for marker in markers:
        name = marker.name.removesuffix(".motion")
        attempts[name] = attempts.pop(name, False) or name != marker.name
    for name, is_motion in attempts.items():
        try:
            config = servers[name]
            motion = config.motion if is_motion else None
            if is_motion and motion is None:
                raise ValueError("motion evidence is not configured")
            report = _read_report(name, config, run_id, timeout_s)
        except (McpServiceError, KeyError, ValueError):
            attention = True
            stop_error = (
                stop_error or f"{name} execution and stop could not be verified"
            )
            continue
        for key, fields in report.evidence.items():
            previous = data.get(key)
            data[key] = {**previous, **fields} if isinstance(previous, dict) else fields
        if (
            report.state != "finished"
            or report.requires_attention
            or (
                motion is not None
                and (
                    report.stop_error
                    or not check_matches(report.evidence, motion.stop_field, True)
                )
            )
        ):
            attention = True
            stop_error = (
                stop_error or report.stop_error or f"{name} stop could not be verified"
            )
        elif motion is not None and not check_matches(
            report.evidence, motion.completion_field, True
        ):
            verification = True
        verification = verification or report.verification_required
        error = error or report.error
    return TaskEvidence(data, attention, verification, error, stop_error)

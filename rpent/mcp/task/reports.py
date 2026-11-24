# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Collect task-local MCP evidence after a child finishes or is interrupted."""

from __future__ import annotations

import json
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from rpent.robots.robot_spec import TaskEvidence
from rpent.tools.mcp_actions import McpActionReceipt, read_action_status
from rpent.tools.mcp_client import (
    McpActionStatusConfig,
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


def _unconfirmed_action(
    name: str, config: McpActionStatusConfig, action_id: str | int
) -> str:
    return f"{name}.{config.status_tool} did not confirm termination of {action_id}"


def has_known_mcp_actions(
    servers: dict[str, McpServerConfig],
    output: Path,
    *,
    legacy_error: str | None = None,
) -> bool:
    """Check that the pending evidence can be re-read by the original action IDs."""
    markers = list((output / ".mcp-actions").glob("*"))
    matched = legacy_error is None
    for marker in markers:
        config = servers.get(marker.name)
        if (
            config is None
            or config.action_status is None
            or config.transport != "streamable-http"
        ):
            return False
        try:
            receipt = McpActionReceipt.model_validate_json(
                marker.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        if receipt.rejection is not None:
            continue
        if receipt.action_id is None:
            return False
        if legacy_error == _unconfirmed_action(
            marker.name, config.action_status, receipt.action_id
        ):
            matched = True
    return bool(markers) and matched


def _read_action_report(
    name: str, config: McpServerConfig, path: Path, timeout_s: float
) -> _RunReport:
    action = config.action_status
    assert action is not None
    receipt = McpActionReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    if receipt.rejection is not None:
        return _RunReport(
            state="finished",
            evidence={"mcp_actions": {name: {"rejection": receipt.rejection}}},
            error=str(
                receipt.rejection.get("error")
                or f"{name}.{action.start_tool} rejected the action"
            ),
        )
    if receipt.action_id is None:
        return _RunReport(
            state="missing",
            stop_error=f"{name}.{action.start_tool} submission could not be verified",
        )
    status = receipt.status
    if status is None and config.transport == "streamable-http":
        bounded = config.model_copy(
            update={
                "startup_timeout_sec": min(config.startup_timeout_sec, 4),
                "tool_timeout_sec": min(config.tool_timeout_sec, 4),
            }
        )
        with closing(McpClient(name, bounded)) as client:
            status = read_action_status(client, action, receipt, timeout_s=timeout_s)
    evidence = {
        "mcp_actions": {name: {"action_id": receipt.action_id, "status": status}}
    }
    if status is None or not receipt.is_terminal(action, status):
        return _RunReport(
            state="missing",
            evidence=evidence,
            stop_error=_unconfirmed_action(name, action, receipt.action_id),
        )
    return _RunReport(
        state="finished",
        evidence=evidence,
        error=None
        if all(
            check_matches(status, key, value) for key, value in action.success.items()
        )
        else f"{name}.{action.status_tool} did not confirm success of {receipt.action_id}",
    )


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
    preflight_file = output / ".mcp-preflight.json"
    if preflight_file.is_file():
        preflight = json.loads(preflight_file.read_text(encoding="utf-8"))
        data["mcp_preflight"] = preflight
        error = error or preflight["error"]
    markers = sorted(
        (output / ".mcp-actions").glob("*"),
        key=lambda marker: marker.stat().st_mtime_ns,
    )
    for marker in markers:
        name = marker.name
        try:
            config = servers[name]
            report = (
                _read_action_report(name, config, marker, timeout_s)
                if config.action_status is not None
                else _read_report(name, config, run_id, timeout_s)
            )
        except (McpServiceError, KeyError, ValueError):
            attention = True
            stop_error = (
                stop_error or f"{name} execution and stop could not be verified"
            )
            continue
        for key, fields in report.evidence.items():
            previous = data.get(key)
            data[key] = {**previous, **fields} if isinstance(previous, dict) else fields
        if report.state != "finished" or report.requires_attention:
            attention = True
            stop_error = (
                stop_error or report.stop_error or f"{name} stop could not be verified"
            )
        verification = verification or report.verification_required
        error = error or report.error
    return TaskEvidence(data, attention, verification, error, stop_error)

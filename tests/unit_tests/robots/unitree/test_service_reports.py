# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Check correlated MCP evidence and the uncertainty rules for completed tasks."""

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from rpent.mcp.task.config import load_deployment
from rpent.mcp.task.reports import collect_run_evidence
from rpent.robots.robot_spec import TaskEvidence
from rpent.tools.mcp_client import McpServerConfig, discover_mcp_tools


def test_only_action_attempts_are_reconciled_by_run_id(
    serve_mcp, tmp_path, monkeypatch
):
    server = FastMCP("report-test", json_response=True)
    run_id = "a" * 32
    reports = {}
    readonly = ToolAnnotations(readOnlyHint=True)

    @server.tool(annotations=readonly)
    def get_pose() -> dict[str, Any]:
        return {"x": 1.0}

    @server.tool()
    def move(ctx: Context) -> dict[str, Any]:
        key = ctx.request_context.meta.model_extra["rpent.dev/run-id"]
        reports[key] = {
            "state": "finished",
            "evidence": {"navigation": {"stop_confirmed": True}},
        }
        return {"ok": True}

    @server.tool(annotations=readonly)
    def get_run_report(ctx: Context) -> dict[str, Any]:
        key = ctx.request_context.meta.model_extra["rpent.dev/run-id"]
        return reports.get(key, {"state": "missing"})

    monkeypatch.setenv("RPENT_MCP_RUN_ID", run_id)
    monkeypatch.setenv("RPENT_TASK_OUTPUT_DIR", str(tmp_path))
    initial = TaskEvidence(data={"navigation": {"completed_navigation_goals": 1}})
    with serve_mcp(server) as url, ExitStack() as connections:
        servers = {"sim": McpServerConfig(url=url, run_report_tool="get_run_report")}
        tools = {
            tool.name: tool
            for tool in discover_mcp_tools(servers, connections, lambda: None)
        }
        assert "mcp__sim__get_run_report" not in tools
        tools["mcp__sim__get_pose"].handler()
        assert (
            collect_run_evidence(
                servers, tmp_path, run_id, timeout_s=1, initial=initial
            )
            == initial
        )

        tools["mcp__sim__move"].handler()
        report = collect_run_evidence(
            servers, tmp_path, run_id, timeout_s=1, initial=initial
        )
        assert not report.requires_attention
        assert report.data["navigation"] == {
            "completed_navigation_goals": 1,
            "stop_confirmed": True,
        }
        missing = collect_run_evidence(
            servers, tmp_path, "b" * 32, timeout_s=1, initial=initial
        )
        assert missing.requires_attention and "stop" in missing.stop_error


@pytest.mark.parametrize(
    ("config_name", "environment", "service", "other_service"),
    [
        ("mcp.toml", "simulation", "mujoco", "go2"),
        ("mcp.go2.toml", "real", "go2", "mujoco"),
    ],
)
def test_unitree_pose_evidence_uses_the_configured_downstream(
    tmp_path, config_name, environment, service, other_service
):
    profile = Path(__file__).resolve().parents[4] / config_name
    deployment = load_deployment(profile)
    assert deployment.robot.environment == environment
    assert "get_pose" in deployment.servers[service].enabled_tools
    pose = {"ok": True, "frame_id": "map", "x": 1.0, "y": 2.0, "yaw": 0.5}
    transcript = {
        "messages": [
            {
                "role": "tool",
                "name": f"mcp__{service}__get_pose",
                "content": json.dumps({"structured_content": pose}),
            },
            {
                "role": "tool",
                "name": f"mcp__{other_service}__get_pose",
                "content": json.dumps({"structured_content": {"ok": True, "x": 90}}),
            },
        ]
    }
    evidence = deployment.execution.read_evidence(tmp_path, transcript)
    assert evidence.data == {"navigation": {"pose": pose}}

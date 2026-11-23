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

from rpent.mcp.task.config import load_deployment, load_run_mcp_servers
from rpent.mcp.task.discovery import check_robot
from rpent.mcp.task.reports import collect_run_evidence
from rpent.robots.robot_spec import TaskEvidence
from rpent.tools.mcp_client import McpServerConfig, discover_mcp_tools

PROFILE = Path(__file__).resolve().parents[4] / "mcp.go2.toml"
RUN_ID = "a" * 32


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


@pytest.fixture
def go2_mcp(serve_mcp):
    server = FastMCP("go2-test", stateless_http=True, json_response=True)
    readonly = ToolAnnotations(readOnlyHint=True)
    pose = {
        "ok": True,
        "frame_id": "map",
        "x": 1.0,
        "y": 2.0,
        "yaw": 0.5,
        "age_s": 0.1,
        "map_id": "room",
        "map_version": "1",
    }
    status = {"ready": False, "reason": "localization_stale"}
    reports = {}

    @server.tool(annotations=readonly)
    def get_pose() -> dict[str, Any]:
        return pose

    @server.tool(annotations=readonly)
    def get_navigation_status() -> dict[str, Any]:
        return status

    @server.tool()
    def follow_plan(plan_id: str, ctx: Context) -> dict[str, Any]:
        run_id = ctx.request_context.meta.model_extra["rpent.dev/run-id"]
        reports[run_id] = {"state": "finished"}
        return {"ok": True, "plan_id": plan_id}

    @server.tool(annotations=readonly)
    def get_run_report(ctx: Context) -> dict[str, Any]:
        run_id = ctx.request_context.meta.model_extra["rpent.dev/run-id"]
        return reports.get(run_id, {"state": "missing"})

    with serve_mcp(server) as url:
        yield url, pose, status, reports


def test_go2_profile_reports_pose_from_the_configured_service(tmp_path, go2_mcp):
    url, pose, _, _ = go2_mcp
    path = tmp_path / "go2.toml"
    path.write_text(
        PROFILE.read_text()
        .replace("http://127.0.0.1:8792/mcp", url)
        .replace("go2.get_pose", "hardware.get_pose")
        .replace("mcp_servers.go2", "mcp_servers.hardware")
        + f'\n[mcp_servers.camera]\nurl = "{url}"\nenabled_tools = ["get_pose"]\n',
        encoding="utf-8",
    )
    deployment = load_deployment(path)
    info = check_robot(deployment, "idle")
    assert info.environment == "real"
    assert {cap.name: cap.status for cap in info.capabilities} == {
        "localization": "ready"
    }
    with ExitStack() as connections:
        tools = {
            tool.name: tool
            for tool in discover_mcp_tools(
                deployment.servers, connections, lambda: None
            )
        }
        assert "mcp__hardware__follow_plan" not in tools
        result = tools["mcp__hardware__get_pose"].handler()

    def message(name, data):
        return {"role": "tool", "name": name, "content": json.dumps(data)}

    evidence = deployment.execution.read_evidence(
        tmp_path,
        {
            "messages": [
                message("mcp__hardware__get_pose", result.data),
                message(
                    "mcp__camera__get_pose",
                    {"structured_content": {"ok": True, "x": 90}},
                ),
            ]
        },
    )
    assert evidence.data == {"navigation": {"pose": pose}}

    pose.update(ok=False, reason="localization_stale")
    localization = check_robot(deployment, "idle").capabilities[0]
    assert localization.status == "unavailable"
    assert "localization_stale" in localization.reason


def test_real_route_checks_readiness_and_requires_stopped_arrival(
    tmp_path, monkeypatch, go2_mcp
):
    url, pose, status, reports = go2_mcp
    path = tmp_path / "go2-motion.toml"
    text = (
        PROFILE.read_text()
        .replace("http://127.0.0.1:8792/mcp", url)
        .replace('"get_navigation_status"]', '"get_navigation_status", "follow_plan"]')
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="requires run_report_tool"):
        load_deployment(path)
    path.write_text(text + 'run_report_tool = "get_run_report"\n', encoding="utf-8")
    servers = load_run_mcp_servers(path)

    monkeypatch.delenv("RPENT_MCP_RUN_ID", raising=False)
    with ExitStack() as connections:
        tools = {
            tool.name: tool
            for tool in discover_mcp_tools(servers, connections, lambda: None)
        }
        untracked = tools["mcp__go2__follow_plan"].handler(plan_id="route")
        assert "requires a tracked task" in untracked.data["error"]
    assert reports == {}

    monkeypatch.setenv("RPENT_MCP_RUN_ID", RUN_ID)
    monkeypatch.setenv("RPENT_TASK_OUTPUT_DIR", str(tmp_path))
    with ExitStack() as connections:
        tools = {
            tool.name: tool
            for tool in discover_mcp_tools(servers, connections, lambda: None)
        }
        follow = tools["mcp__go2__follow_plan"].handler
        assert tools["mcp__go2__get_pose"].handler().data["structured_content"] == pose
        rejected = follow(plan_id="route").data
        assert "localization_stale" in rejected["error"]
        evidence_reader = load_deployment(path).execution.read_evidence
        evidence = evidence_reader(
            tmp_path,
            {
                "messages": [
                    {
                        "role": "tool",
                        "name": "mcp__go2__follow_plan",
                        "content": json.dumps(rejected),
                    }
                ]
            },
        )
        assert evidence.error == rejected["error"]
        assert not (tmp_path / ".mcp-actions").exists()
        assert reports == {}
        status.update(ready=True, reason="")
        assert follow(plan_id="route").data["structured_content"]["ok"] is True

    def collect():
        return collect_run_evidence(
            servers, tmp_path, RUN_ID, timeout_s=1, initial=TaskEvidence()
        )

    assert collect().requires_attention
    reports[RUN_ID]["evidence"] = {"navigation": {"stop_confirmed": True}}
    evidence = collect()
    assert not evidence.requires_attention and evidence.verification_required
    reports[RUN_ID]["evidence"]["navigation"].update(navigation_completed=True)
    evidence = collect()
    assert not evidence.requires_attention and not evidence.verification_required
    assert evidence.data["navigation"] == {
        "stop_confirmed": True,
        "navigation_completed": True,
    }

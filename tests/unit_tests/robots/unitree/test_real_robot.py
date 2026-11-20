# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

"""Verify the documented asynchronous Go2 API using offline HTTP responses."""

import io
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from rpent.tools.mcp_client import McpClient, McpServerConfig
from rpent.tools.toolkit import ToolCancelled

MOVE = {"dx_m": 0.3, "dy_m": -0.1, "arrival_turn_deg": 30}


class CommunicationService:
    """A test receiver implementing the documented queue and feedback records."""

    def __init__(self):
        self.requests = []
        self.records = {}
        self.mode = "complete"
        self.connection_epoch = 1795060800
        self.stop_accepted = False
        self.queued = threading.Event()

    @property
    def methods(self):
        return [entry["method"] for entry in self.requests]

    def handle(self, payload):
        self.requests.append(payload)
        method = payload["method"]
        assert payload["session_id"] is None
        status = {
            "robot_index": 1,
            "status": "online",
            "connected": True,
            "heartbeat_healthy": True,
            "connected_at_unix": self.connection_epoch,
            "controller_ready": None,
            "camera": {"ready": True, "stale": False},
        }
        if method == "env.list_robots":
            return {"ok": True, "result": {"total": 1, "robots": [status]}}
        if method == "healthz":
            assert payload["kwargs"]["robot_index"] == 1
            return {"ok": True, "result": status}
        if method == "env.get_request":
            record = self.records[payload["kwargs"]["request_id"]]
            if record["operation"] == "move_relative" and record["status"] == "queued":
                if self.stop_accepted:
                    self._complete_move(record, stopped=True)
                elif self.mode == "complete":
                    self._complete_move(record, stopped=False)
            return {"ok": True, "result": record}

        assert payload["kwargs"] == {"robot_index": 1}
        assert payload["idempotency_key"] == payload["request_id"]
        assert all(
            p.get("idempotency_key") != payload["idempotency_key"]
            for p in self.requests[:-1]
        )
        request_id = str(uuid4())
        operation = {
            "env.execute_command": "move_relative",
            "env.capture_image": "capture_image",
            "env.stop": "stop",
        }[method]
        record = {
            "robot_index": 1,
            "operation": operation,
            "request_id": request_id,
            "client_request_id": payload["request_id"],
            "status": "queued",
            "outcome_known": False,
            "result_available": False,
            "result": None,
            "error": None,
        }
        self.records[request_id] = record
        if operation == "move_relative":
            assert payload["args"] == [{"type": "move_relative", "payload": MOVE}]
            if self.mode == "lost_ack":
                raise httpx.ReadTimeout("response lost after queueing")
            if self.mode == "rejected":
                record.update(status="rejected", outcome_known=True, error="queue full")
                return {"ok": False, "request_id": request_id, "error": "queue full"}
            self.queued.set()
        elif operation == "capture_image":
            assert payload["args"] == [{"rgbd": True, "after_motion": True}]
            self._finish(
                record,
                "capture_result",
                status="captured",
                rgbd=True,
                exit_code=0,
                files={"color": "/robot/color.jpg", "depth": "/robot/depth.png"},
            )
            record.update(image_location="robot_local", image_content_available=False)
        else:
            assert payload["args"] == []
            self.stop_accepted = True
            self._finish(
                record,
                "stop_result",
                status="completed",
                ok=True,
                stop_requested=True,
                stop_confirmed=False,
                active_task_may_continue=True,
            )
        return {"ok": True, "result": record}

    @staticmethod
    def _finish(record, kind, **feedback):
        record.update(
            status="completed",
            outcome_known=True,
            result_available=True,
            result={
                "type": kind,
                "request_id": record["request_id"],
                "robot_index": 1,
                **feedback,
            },
        )

    def _complete_move(self, record, *, stopped):
        self._finish(
            record,
            "command_result",
            status="failed" if stopped else "completed",
            completed=not stopped,
            dry_run=False,
            exit_code=2 if stopped else 0,
            reason="shared stop requested" if stopped else "completed",
            result={
                "success": not stopped,
                "arrival_phase_started": not stopped,
                "stop_ack": True,
                "final_state_available": True,
                "dx_m": 0.15 if stopped else 0.3,
                "dy_m": -0.1,
            },
            stdout="large controller log",
        )
        if stopped:
            record["status"] = "failed"


@pytest.fixture
def communication():
    return CommunicationService()


@pytest.fixture
def communication_endpoint(communication):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/call"
            try:
                reply = communication.handle(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
            except httpx.ReadTimeout:
                self.close_connection = True
                return
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=2)


def go2_args(endpoint):
    return [
        "-m",
        "robots.unitree.mcp.real.server",
        "--endpoint",
        endpoint,
        "--robot-index",
        "1",
    ]


@pytest.fixture
def go2_mcp(tmp_path, communication_endpoint, monkeypatch):
    out = tmp_path / "run"
    monkeypatch.setenv("RPENT_TASK_OUTPUT_DIR", str(out))
    config = McpServerConfig(
        transport="stdio",
        command=sys.executable,
        args=go2_args(communication_endpoint) + ["--action-timeout-s", "3"],
        env_from=["RPENT_TASK_OUTPUT_DIR"],
        tool_timeout_sec=8,
    )
    with closing(McpClient("real", config)) as client:
        yield client, out / "real/latest.json"


def test_failed_submission_with_server_id_is_reconciled_without_replay(
    go2_mcp, communication
):
    client, state = go2_mcp
    communication.mode = "rejected"
    result = client.call_tool("move_relative", MOVE)
    assert result.isError and "queue full" in result.content[0].text
    record = json.loads(state.read_text())
    assert record["last_move_status"] == "rejected"
    request_id = record["last_move_request_id"]
    assert request_id != communication.records[request_id]["client_request_id"]
    assert communication.methods == [
        "healthz",
        "env.execute_command",
        "env.get_request",
    ]
    assert record["error"]


def test_lost_move_acknowledgement_blocks_new_motion(go2_mcp, communication):
    client, path = go2_mcp
    communication.mode = "lost_ack"
    assert client.call_tool("move_relative", MOVE).isError
    assert client.call_tool("move_relative", MOVE).isError
    state = json.loads(path.read_text())
    assert state["submission_uncertain"] and state["stop_error"]
    assert state["stop_request_id"]
    assert communication.methods.count("env.execute_command") == 1
    assert communication.methods.count("env.stop") == 1


def test_cancelling_a_queued_move_checks_the_original_stop_feedback(
    go2_mcp, communication
):
    client, state_path = go2_mcp
    communication.mode = "hold"
    cancelled = threading.Event()

    def check_cancelled():
        if cancelled.is_set():
            raise ToolCancelled("robot move cancelled")

    with ThreadPoolExecutor(max_workers=1) as pool:
        move = pool.submit(
            client.call_tool, "move_relative", MOVE, check_cancelled=check_cancelled
        )
        assert communication.queued.wait(timeout=2)
        cancelled.set()
        with pytest.raises(ToolCancelled):
            move.result(timeout=3)
    client.close()  # As on toolkit shutdown, EOF cancels the server-side request.
    assert communication.methods.count("env.stop") == 1
    assert communication.methods[-1] == "env.get_request"
    queried = communication.requests[-1]["kwargs"]["request_id"]
    assert communication.records[queried]["operation"] == "move_relative"
    assert communication.records[queried]["result"]["reason"] == "shared stop requested"
    state = json.loads(state_path.read_text())
    assert state["active_request_id"] is None and state["stop_error"] is None
    assert state["stop_confirmed"] is True and state["last_move_request_id"] == queried
    assert state["completed_moves"] == 0


def test_stop_acknowledgement_alone_does_not_mean_stopped(go2_mcp, communication):
    client, path = go2_mcp
    assert client.call_tool("stop_robot", {}).isError
    state = json.loads(path.read_text())
    assert state["stop_confirmed"] is False
    assert state["stop_request_id"] in communication.records
    assert state["stop_error"]


def test_connection_change_blocks_later_motion_in_the_same_run(go2_mcp, communication):
    client, _ = go2_mcp
    assert not client.call_tool("move_relative", MOVE).isError
    communication.connection_epoch += 1
    result = client.call_tool("move_relative", MOVE)
    assert result.isError and "reconnected" in result.content[0].text
    assert communication.methods.count("env.execute_command") == 1


def test_motion_bound_is_checked_before_sending(go2_mcp, communication):
    client, _ = go2_mcp
    move = {**MOVE, "dx_m": 4.0, "dy_m": 4.0}
    assert client.call_tool("move_relative", move).isError
    assert "env.execute_command" not in communication.methods


def test_public_cli_records_real_actions_through_mcp(
    tmp_path, communication, communication_endpoint, monkeypatch
):
    from rpent.cli import main as cli

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    plans = iter(
        [
            ("mcp__real__get_robot_status", {}),
            ("mcp__real__move_relative", MOVE),
            ("mcp__real__capture_image", {"rgbd": True, "after_motion": True}),
            ("finish", {"status": "success", "summary": "移动完成，照片保存于本体"}),
        ]
    )

    async def stream(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert {
            "mcp__real__move_relative",
            "mcp__real__get_robot_request",
            "mcp__real__stop_robot",
            "mcp__real__capture_image",
        } <= names
        assert "follow_navigation" not in names
        name, arguments = next(plans)
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments))}

    monkeypatch.setattr(
        "rpent.planner.base.build_api_model",
        lambda *a, **k: FunctionModel(stream_function=stream),
    )
    monkeypatch.setattr(sys, "stdin", Terminal())
    out = tmp_path / "run"
    monkeypatch.setenv("RPENT_TASK_OUTPUT_DIR", str(out))
    mcp_config = tmp_path / "mcp.toml"
    mcp_config.write_text(
        '[mcp_servers.real]\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps(go2_args(communication_endpoint))}\n"
        'env_from = ["RPENT_TASK_OUTPUT_DIR"]\n'
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rpent",
            "--robot",
            "unitree",
            "--mcp-config",
            str(mcp_config),
            "--planner",
            "api",
            "--model",
            "openai-chat:offline-go2",
            "--reasoning-effort",
            "none",
            "--max-turns",
            "4",
            "--planner-timeout-s",
            "12",
            "--task",
            "前进并抓拍",
            "--output-dir",
            str(out),
            "--memory-dir",
            str(tmp_path / "memory"),
        ],
    )
    assert cli.main() == 0
    record = json.loads(next(out.glob("transcript_unitree_*.json")).read_text())
    assert record["validation_scope"] == "unitree_mcp"
    assert record["finish"]["status"] == "success" and record["error"] is None
    assert communication.methods.count("env.execute_command") == 1
    assert communication.methods.count("env.get_request") == 1
    assert communication.methods.count("env.capture_image") == 1
    assert "/robot/color.jpg" in json.dumps(record["messages"])
    assert json.loads((out / "real/latest.json").read_text())["completed_moves"] == 1

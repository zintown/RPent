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

"""Opt-in MCP -> public RPent CLI -> real LightNav -> Go2 integration checks."""

from __future__ import annotations

import json
import math
import os
import shlex
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn

from robots.unitree.mcp.task.server import create_app
from tests.e2e_tests.offline_planner_server import (
    OFFLINE_MODEL_NAME,
    OfflinePlannerServer,
    ScriptedToolCall,
)

REPO = Path(__file__).resolve().parents[3]
PROJECT = REPO.parent
pytestmark = pytest.mark.skipif(
    os.environ.get("RPENT_RUN_UNITREE_VLN") != "1",
    reason="requires operator-started Go2, observation and LightNav services",
)


@contextmanager
def task_api(jobs):
    """Open an MCP connection to the runner that owns the TTY child."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
    app = create_app(repo_root=REPO, jobs_dir=jobs, robot_id="go2-vln-test")
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(
        target=lambda: server.run(sockets=[listener]), daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        yield endpoint
    finally:
        server.should_exit = True
        thread.join(40)
        listener.close()
        assert not thread.is_alive(), "owned task API did not drain its child"


def run_task(endpoint, output, *, cancel, origin):
    """Call run_task and exercise standard MCP cancellation for its in-flight request."""
    mcp = endpoint + "/mcp"
    headers = {"Accept": "application/json, text/event-stream"}
    samples = []
    request_id = 1

    def post(http, method, params, *, request_id=None):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        return http.post(mcp, json=message, headers=headers).raise_for_status()

    with httpx.Client(trust_env=False, timeout=75) as http:
        response = post(
            http,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "vln-policy-test", "version": "1"},
            },
            request_id=0,
        )
        headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
        headers["MCP-Protocol-Version"] = response.json()["result"]["protocolVersion"]
        post(http, "notifications/initialized", {})
        requested_stop = False

        def stop():
            post(http, "notifications/cancelled", {"requestId": request_id})

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                post,
                http,
                "tools/call",
                {
                    "name": "run_task",
                    "arguments": {"instruction": "bounded VLN integration"},
                },
                request_id=request_id,
            )
            try:
                deadline = time.monotonic() + 70
                while not pending.done() and time.monotonic() < deadline:
                    if not cancel:
                        time.sleep(0.05)
                        continue
                    state_path = next(
                        output.glob("runs/*/navigation/vln/latest.json"), None
                    )
                    if state_path is None:
                        time.sleep(0.05)
                        continue
                    vln = json.loads(state_path.read_text())
                    pose = vln.get("pose") or origin
                    yaw_delta = pose["yaw"] - origin["yaw"]
                    moved = (
                        math.hypot(pose["x"] - origin["x"], pose["y"] - origin["y"])
                        > 0.02
                        or abs(math.atan2(math.sin(yaw_delta), math.cos(yaw_delta)))
                        > 0.02
                    )
                    samples.append(
                        {
                            "time": time.time(),
                            "vln_state": vln.get("state"),
                            "predictions": vln.get("predictions"),
                            "active_control_id": vln.get("active_control_id"),
                            "pose": pose,
                        }
                    )
                    if (
                        vln.get("predictions", 0) >= 5
                        and vln.get("active_control_id")
                        and moved
                    ):
                        stop()
                        requested_stop = True
                        break
                    time.sleep(0.05)
                if requested_stop:
                    while http.get(endpoint + "/healthz", timeout=3).json()["busy"]:
                        assert time.monotonic() < deadline, "owned task did not stop"
                        time.sleep(0.05)
                else:
                    result = pending.result(timeout=2).json()["result"]
                    assert not result.get("isError"), result
                    assert result["structuredContent"]["status"] == "succeeded"
            finally:
                if not pending.done() and not requested_stop:
                    stop()
                http.delete(mcp, headers=headers).raise_for_status()
        [job_path] = list((output / "jobs").glob("*/job.json"))
        job = json.loads(job_path.read_text())
    (output / "samples.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=2) + "\n"
    )
    (output / "job-result.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2) + "\n"
    )
    if cancel:
        assert requested_stop, job
    return job, samples


def run_case(monkeypatch, *, cancel):
    """Exercise real inference, capture alignment, task status and bounded stopping."""
    sim = "http://127.0.0.1:8770"
    observation = "http://127.0.0.1:8771"
    model = os.environ.get("RPENT_UNITREE_VLN_ENDPOINT", "ws://127.0.0.1:8050")
    output = Path(
        os.environ.get(
            "RPENT_UNITREE_VLN_OUTPUT",
            str(PROJECT / "output/vln-integration/policy-chain"),
        )
    )
    output = output / (("cancel-" if cancel else "hold-") + uuid4().hex[:10])
    output.mkdir(parents=True)
    with httpx.Client(trust_env=False, timeout=10) as http:
        health = http.get(sim + "/health").json()
        assert health["control_mode"] == "idle" and not health["other_controllers"]
        origin = http.get(sim + "/pose").json()
        goal = {k: origin[k] for k in ("x", "y", "yaw")}
        action = {
            "instruction": "Turn left and continue inspecting the room."
            if cancel
            else "Stop here.",
            "timeout_s": 35.0,
        }
        if not cancel:
            action["expected_goal"] = goal
        script = (
            ScriptedToolCall("mcp__vln__run_vln_navigation", action),
            ScriptedToolCall(
                "finish",
                {
                    "status": "success" if not cancel else "stuck",
                    "summary": "bounded integration only",
                },
            ),
        )
        config_path = output / "mcp.toml"
        config_path.write_text(
            "[mcp_servers.vln]\n"
            'transport = "stdio"\n'
            f"command = {json.dumps(sys.executable)}\n"
            f"cwd = {json.dumps(str(REPO))}\n"
            'env_from = ["RPENT_TASK_OUTPUT_DIR"]\n'
            "tool_timeout_sec = 60\n"
            f"args = {json.dumps(['-m', 'robots.unitree.mcp.vln.server', '--sim-endpoint', sim, '--observation-endpoint', observation, '--vln-endpoint', model, '--vln-timeout-s', '40'])}\n"
        )
        with OfflinePlannerServer(script) as planner:
            config = {
                "RPENT_VENV_DIR": str(PROJECT / ".venv"),
                "RPENT_MCP_CONFIG": str(config_path),
                "RPENT_RUNS_DIR": str(output / "runs"),
                "RPENT_MODEL": f"openai-chat:{OFFLINE_MODEL_NAME}",
                "RPENT_MEMORY_DIR": str(output / "memory"),
                "RPENT_MAX_TURNS": "5",
                "RPENT_PLANNER_TIMEOUT_S": "90",
                "OPENAI_API_KEY": OFFLINE_MODEL_NAME,
                "OPENAI_BASE_URL": planner.base_url,
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "PYDANTIC_AI_NO_BANNER": "1",
            }
            config_path = output / "test.env"
            config_path.write_text(
                "".join(
                    f"export {key}={shlex.quote(value)}\n"
                    for key, value in config.items()
                )
            )
            config_path.chmod(0o600)
            monkeypatch.setenv("RPENT_CONFIG_FILE", str(config_path))
            with task_api(output / "jobs") as endpoint:
                job, samples = run_task(endpoint, output, cancel=cancel, origin=origin)
                vln = job["navigation"]["vln"]
                assert job["status"] == ("stopped" if cancel else "succeeded"), job
                assert vln["predictions"] >= 1 and vln["accepted_references"] >= 1
                assert vln["stop_confirmed"] and vln["active_control_id"] is None
                if cancel:
                    assert vln["state"] == "cancelled", job
                else:
                    assert vln["goal_verified"] and vln["state"] == "finished"
                    planner.assert_complete()
                final_health = http.get(sim + "/health").json()
                assert (
                    final_health["control_mode"] == "idle"
                    and not final_health["other_controllers"]
                )
                assert not http.get(endpoint + "/healthz").json()["busy"]
            (output / "verification.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "scope": "MCP task runner and scripted planner; real LightNav model, Go2 images, MPC and DDS",
                        "goal_scope": "cancellation during motion"
                        if cancel
                        else "hold the measured starting map pose; not a visual navigation benchmark",
                        "task": job,
                        "samples": len(samples),
                        "final_health": final_health,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
    return output


def test_vln_mcp_policy_chain_holds_known_pose_and_reports_verified_stop(monkeypatch):
    run_case(monkeypatch, cancel=False)


def test_vln_mcp_cancellation_stops_simulated_motion_and_releases_control(monkeypatch):
    run_case(monkeypatch, cancel=True)

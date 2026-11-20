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
import os
import shlex
import socket
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn

from robots.unitree.task_server import create_app
from rpent.tools.mcp_client import McpClient, McpServerConfig
from tests.e2e_tests.offline_planner_server import (
    OFFLINE_MODEL_NAME,
    OfflinePlannerServer,
    ScriptedToolCall,
)

REPO = Path(__file__).resolve().parents[3]
PROJECT = REPO.parent
pytestmark = pytest.mark.skipif(
    os.environ.get("RPENT_RUN_UNITREE_VLN") != "1",
    reason="requires the operator-started Go2, A* and real LightNav services",
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
        with closing(
            McpClient("rpent", McpServerConfig(url=endpoint + "/mcp/"))
        ) as client:
            yield endpoint, client
    finally:
        server.should_exit = True
        thread.join(40)
        listener.close()
        assert not thread.is_alive(), "owned task API did not drain its child"


def run_case(monkeypatch, *, cancel):
    """Exercise real inference, capture alignment, task status and bounded stopping."""
    sim = "http://127.0.0.1:8770"
    nav = "http://127.0.0.1:8765"
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
    identity = {"robot_id": "go2-vln-test", "task_id": output.name}
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
            ScriptedToolCall("run_vln_navigation", action),
            ScriptedToolCall(
                "finish",
                {
                    "status": "success" if not cancel else "stuck",
                    "summary": "bounded integration only",
                },
            ),
        )
        samples = []
        with OfflinePlannerServer(script) as planner:
            config = {
                "RPENT_VENV_DIR": str(PROJECT / ".venv"),
                "RPENT_NAV_ENDPOINT": nav,
                "RPENT_SIM_ENDPOINT": sim,
                "RPENT_VLN_ENDPOINT": model,
                "RPENT_OBSERVATION_ENDPOINT": observation,
                "RPENT_RUNS_DIR": str(output / "runs"),
                "RPENT_MODEL": f"openai-chat:{OFFLINE_MODEL_NAME}",
                "RPENT_MEMORY_DIR": str(output / "memory"),
                "RPENT_MAX_TURNS": "5",
                "RPENT_PLANNER_TIMEOUT_S": "90",
                "RPENT_VLN_TIMEOUT_S": "40",
                "RPENT_VLN_INFERENCE_TIMEOUT_S": "3",
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
            monkeypatch.setenv("RPENT_UNITREE_MODE", "mujoco")
            with task_api(output / "jobs") as (endpoint, client):

                def call(name, **arguments):
                    result = client.call_tool(name, arguments)
                    assert not result.isError, result
                    return result.structuredContent

                started = call(
                    "submit_robot_task",
                    **identity,
                    task_name="bounded VLN integration",
                )
                assert started["created"]
                job = started["task"]
                deadline = time.monotonic() + 75
                requested_stop = False
                try:
                    while time.monotonic() < deadline:
                        job = call("get_robot_task", **identity)
                        vln = (job.get("navigation") or {}).get("vln") or {}
                        applied = (vln.get("control") or {}).get("applied") or {}
                        samples.append(
                            {
                                "time": time.time(),
                                "task_status": job["status"],
                                "vln_state": vln.get("state"),
                                "predictions": vln.get("predictions"),
                                "active_control_id": vln.get("active_control_id"),
                                "applied": applied,
                            }
                        )
                        if (
                            cancel
                            and not requested_stop
                            and vln.get("predictions", 0) >= 5
                            and max(
                                abs(applied.get("vx", 0)), abs(applied.get("wz", 0))
                            )
                            > 0.02
                        ):
                            call("stop_robot_task", **identity)
                            requested_stop = True
                        if job["status"] not in {"starting", "running", "stopping"}:
                            break
                        time.sleep(0.05)
                    else:
                        raise AssertionError("bounded VLN MCP task timed out")
                finally:
                    if job["status"] in {"starting", "running"}:
                        call("stop_robot_task", **identity)
                    (output / "samples.json").write_text(
                        json.dumps(samples, ensure_ascii=False, indent=2) + "\n"
                    )
                    (output / "job-result.json").write_text(
                        json.dumps(job, ensure_ascii=False, indent=2) + "\n"
                    )
                vln = job["navigation"]["vln"]
                assert job["status"] == ("stopped" if cancel else "succeeded"), job
                assert vln["predictions"] >= 1 and vln["accepted_references"] >= 1
                assert vln["stop_confirmed"] and vln["active_control_id"] is None
                if cancel:
                    assert requested_stop and vln["state"] == "cancelled", job
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

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

"""Robot registration and CLI hooks for the Unitree runtimes."""

from __future__ import annotations

import argparse
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from robots.unitree.prompt_bundle import system_prompt, user_prompt
from rpent.dashboard.events import DashboardEventSink
from rpent.memory import MemoryManager
from rpent.robots.prompt_bundle import PromptBundle
from rpent.robots.robot_spec import RobotSpec, RunConfig
from rpent.utils.config import get_memory_dir, get_repo_root
from rpent.utils.daemon import ProcessDaemon


def get_robot_spec() -> RobotSpec:
    """Register a terminal-only Unitree backend with free-form task input."""
    return RobotSpec(
        name="unitree",
        is_real_robot=True,
        prompts=PromptBundle(system=system_prompt, user=user_prompt),
        add_cli_args=_add_cli_args,
        parse_config=_parse_config,
        init_runtime=_init_runtime,
    )


def get_toolkit(
    *,
    runtime_kwargs: dict[str, Any],
    dashboard_events: DashboardEventSink,
    config: RunConfig,
):
    """Build the session toolkit for the selected Unitree mode."""
    mode = config.prompt_vars["unitree_mode"]
    if mode == "mcp":
        from rpent.session import EnvState
        from rpent.tools import Toolkit

        return Toolkit(
            dashboard_events=dashboard_events,
            memory=MemoryManager(config.prompt_vars["memory_dir"]),
            state=EnvState(config.output_dir),
            require_finish=True,
        )
    if mode == "real":
        from robots.unitree.real_toolkit import RealRobotToolkit

        real = runtime_kwargs["real"]
        try:
            return RealRobotToolkit(
                real=real,
                dashboard_events=dashboard_events,
                memory=MemoryManager(config.prompt_vars["memory_dir"]),
                state_output_dir=config.output_dir,
                action_timeout_s=config.prompt_vars["real_action_timeout_s"],
            )
        except Exception:
            real.close()
            raise
    from robots.unitree.mujoco_toolkit import UnitreeMujocoToolkit

    try:
        return UnitreeMujocoToolkit(
            **runtime_kwargs,
            dashboard_events=dashboard_events,
            memory=MemoryManager(config.prompt_vars["memory_dir"]),
            state_output_dir=config.output_dir,
            navigation_timeout_s=config.prompt_vars["navigation_timeout_s"],
            vln_timeout_s=config.prompt_vars.get("vln_timeout_s", 120.0),
        )
    except Exception:
        for client in runtime_kwargs.values():
            client.close()
        raise


def _add_cli_args(parser: argparse.ArgumentParser, use_dashboard: bool) -> None:
    parser.set_defaults(memory_profile="local")
    parser.add_argument(
        "--task", required=not use_dashboard, help="Natural-language robot task."
    )
    parser.add_argument(
        "--unitree-mode",
        choices=("mcp", "mujoco", "real"),
        default="mcp",
        help="Select MCP simulation (default), MuJoCo HTTP/VLN, or physical Go2.",
    )
    parser.add_argument(
        "--nav-endpoint",
        default="http://192.168.104.191:8765",
        help="A* planning service base URL, for --unitree-mode mujoco.",
    )
    parser.add_argument(
        "--sim-endpoint",
        default="http://192.168.104.191:8770",
        help="Simulation control base URL, for --unitree-mode mujoco.",
    )
    parser.add_argument(
        "--navigation-timeout-s",
        type=float,
        default=300.0,
        help="Per-walk or preview budget in MuJoCo mode (default: 300 seconds).",
    )
    parser.add_argument(
        "--vln-endpoint",
        default=None,
        help="Optional LightNav ws:// or wss:// endpoint in MuJoCo mode.",
    )
    parser.add_argument(
        "--observation-endpoint",
        default=None,
        help="First-person HTTP service; required with --vln-endpoint.",
    )
    parser.add_argument(
        "--vln-timeout-s",
        type=float,
        default=120.0,
        help="Maximum budget for one VLN episode (default: 120 seconds).",
    )
    parser.add_argument(
        "--vln-inference-timeout-s",
        type=float,
        default=3.0,
        help="Timeout for one LightNav request (default: 3 seconds).",
    )
    parser.add_argument(
        "--robot-index",
        type=int,
        default=None,
        help="Required robot index in real mode.",
    )
    parser.add_argument(
        "--real-endpoint",
        default=None,
        help="HTTP base URL of the physical Go2 communication service (port 9001).",
    )
    parser.add_argument(
        "--real-action-timeout-s",
        type=float,
        default=180.0,
        help="Time to wait for a physical move or capture result (default: 180 seconds).",
    )


def _parse_config(args: argparse.Namespace) -> RunConfig:
    task = (args.task or "").strip()
    if not task:
        raise ValueError("--task must contain a task description")
    if args.memory_profile != "local":
        raise ValueError("Unitree runs require --memory-profile local")
    unitree_mode = args.unitree_mode
    if unitree_mode == "mcp" and not any(
        server.enabled and server.enabled_tools != []
        for server in getattr(args, "mcp_servers", {}).values()
    ):
        raise ValueError("MCP simulation requires enabled tools in --mcp-config")
    if unitree_mode == "real":
        from robots.unitree.http_client import validate_endpoint
        from robots.unitree.real_client import validate_robot_index

        if not args.real_endpoint or args.robot_index is None:
            raise ValueError("real mode requires --real-endpoint and --robot-index")
        validate_robot_index(args.robot_index)
        validate_endpoint(args.real_endpoint)
        if args.real_endpoint.rstrip("/").endswith("/call"):
            raise ValueError("--real-endpoint is a base URL; omit /call")
        if (
            not math.isfinite(args.real_action_timeout_s)
            or args.real_action_timeout_s <= 0
        ):
            raise ValueError("--real-action-timeout-s must be finite and positive")
    elif args.real_endpoint is not None or args.robot_index is not None:
        raise ValueError(
            "--real-endpoint and --robot-index require --unitree-mode real"
        )
    vln_endpoint = getattr(args, "vln_endpoint", None)
    observation_endpoint = getattr(args, "observation_endpoint", None)
    if (vln_endpoint is None) != (observation_endpoint is None):
        raise ValueError(
            "--vln-endpoint and --observation-endpoint must be provided together"
        )
    if vln_endpoint is not None:
        from robots.unitree.http_client import validate_endpoint
        from robots.unitree.vln_client import validate_vln_endpoint

        if unitree_mode != "mujoco":
            raise ValueError("VLN requires --unitree-mode mujoco")
        validate_vln_endpoint(vln_endpoint)
        validate_endpoint(observation_endpoint)
        for value in (args.vln_timeout_s, args.vln_inference_timeout_s):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("VLN budgets must be finite and positive")
    if unitree_mode == "mujoco":
        from robots.unitree.http_client import validate_endpoint

        validate_endpoint(args.nav_endpoint)
        validate_endpoint(args.sim_endpoint)
        if (
            not math.isfinite(args.navigation_timeout_s)
            or args.navigation_timeout_s <= 0
        ):
            raise ValueError("--navigation-timeout-s must be finite and positive")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(
        args.output_dir or get_repo_root() / "logs" / f"{timestamp}_unitree"
    )
    memory_dir = (
        Path(args.memory_dir).expanduser().resolve()
        if args.memory_dir
        else get_memory_dir("unitree")
    )
    mode_vars = {"unitree_mode": unitree_mode}
    if unitree_mode == "real":
        mode_vars.update(
            env_endpoint=args.real_endpoint,
            real_endpoint=args.real_endpoint,
            real_action_timeout_s=args.real_action_timeout_s,
        )
    if unitree_mode == "mujoco":
        mode_vars.update(
            nav_endpoint=args.nav_endpoint,
            sim_endpoint=args.sim_endpoint,
            navigation_timeout_s=args.navigation_timeout_s,
        )
        if vln_endpoint is not None:
            mode_vars.update(
                vln_endpoint=vln_endpoint,
                observation_endpoint=observation_endpoint,
                vln_timeout_s=args.vln_timeout_s,
                vln_inference_timeout_s=args.vln_inference_timeout_s,
            )
    return RunConfig(
        recipe_tag=f"unitree_{uuid4().hex[:12]}",
        output_dir=output_dir,
        prompt_vars={
            "task": task,
            "robot_index": args.robot_index,
            "memory_dir": str(memory_dir),
            **mode_vars,
        },
        task_desc={
            "task": task,
            "robot_index": args.robot_index,
            "validation_scope": {
                "mujoco": "mujoco_navigation",
                "mcp": "mujoco_mcp_navigation",
                "real": "real_relative_actions",
            }[unitree_mode],
            **mode_vars,
        },
    )


def _init_runtime(
    args: argparse.Namespace,
    output_dir: Path,
    dashboard_events: DashboardEventSink,
    components: set[str] | None,
) -> tuple[list[ProcessDaemon], dict[str, Any]]:
    if args.unitree_mode == "mcp":
        return [], {}
    if args.unitree_mode == "mujoco":
        return _init_mujoco_runtime(args, components)
    from robots.unitree.real_client import RealRobotClient

    if components is not None:
        if components.difference({"real"}):
            raise ValueError(f"unknown real runtime components: {sorted(components)}")
        if not components:
            return [], {}
    real = RealRobotClient(args.real_endpoint, robot_index=args.robot_index)
    try:
        real.list_robots()
    except Exception:
        real.close()
        raise
    return [], {"real": real}


def _init_mujoco_runtime(
    args: argparse.Namespace, components: set[str] | None
) -> tuple[list[ProcessDaemon], dict[str, Any]]:
    """Connect external simulation and navigation services with owned clients."""
    from robots.unitree.http_client import map_identity
    from robots.unitree.navigation_client import NavigationClient
    from robots.unitree.simulation_client import SimulationClient

    enabled_vln = getattr(args, "vln_endpoint", None) is not None
    available = {"nav", "sim", "vln", "observation"} if enabled_vln else {"nav", "sim"}
    selected = available if components is None else components
    if selected.difference(available):
        raise ValueError(f"unknown MuJoCo runtime components: {sorted(selected)}")
    clients: dict[str, Any] = {}
    try:
        if "nav" in selected:
            clients["nav"] = NavigationClient(args.nav_endpoint)
        if "sim" in selected:
            clients["sim"] = SimulationClient(args.sim_endpoint, timeout_s=5.0)
        identities = [map_identity(client.health()) for client in clients.values()]
        if len(set(identities)) > 1:
            raise ValueError("planning and simulation map_id/map_version do not match")
        if "observation" in selected:
            from robots.unitree.observation_client import ObservationClient

            clients["observation"] = ObservationClient(
                args.observation_endpoint, timeout_s=3.0
            )
            identity = map_identity(clients["observation"].observe())
            if identities and identity != identities[0]:
                raise ValueError(
                    "camera and simulation map_id/map_version do not match"
                )
        if "vln" in selected:
            from robots.unitree.vln_client import VlnClient

            clients["vln"] = VlnClient(
                args.vln_endpoint, timeout_s=args.vln_inference_timeout_s
            )
            clients["vln"].health()
    except Exception:
        for client in clients.values():
            client.close()
        raise
    return [], clients

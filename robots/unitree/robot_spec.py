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

"""Register the Unitree task runner and its MCP toolkit."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from robots.unitree.prompt_bundle import system_prompt, user_prompt
from rpent.dashboard.events import DashboardEventSink
from rpent.memory import MemoryManager
from rpent.robots.prompt_bundle import PromptBundle
from rpent.robots.robot_spec import RobotSpec, RunConfig
from rpent.session import EnvState
from rpent.tools import Toolkit
from rpent.utils.config import get_memory_dir, get_repo_root
from rpent.utils.daemon import ProcessDaemon


def get_robot_spec() -> RobotSpec:
    """Register the terminal task entry point for Unitree."""
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
) -> Toolkit:
    """Use common task tools and the MCP services discovered by the CLI."""
    return Toolkit(
        dashboard_events=dashboard_events,
        memory=MemoryManager(config.prompt_vars["memory_dir"]),
        state=EnvState(config.output_dir),
        require_finish=True,
    )


def _add_cli_args(parser: argparse.ArgumentParser, use_dashboard: bool) -> None:
    parser.set_defaults(memory_profile="local")
    parser.add_argument(
        "--task", required=not use_dashboard, help="Natural-language robot task."
    )


def _parse_config(args: argparse.Namespace) -> RunConfig:
    task = (args.task or "").strip()
    if not task:
        raise ValueError("--task must contain a task description")
    if args.memory_profile != "local":
        raise ValueError("Unitree runs require --memory-profile local")
    if not any(
        server.enabled and server.enabled_tools != []
        for server in getattr(args, "mcp_servers", {}).values()
    ):
        raise ValueError("Unitree requires enabled tools in --mcp-config")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(
        args.output_dir or get_repo_root() / "logs" / f"{timestamp}_unitree"
    )
    memory_dir = (
        Path(args.memory_dir).expanduser().resolve()
        if args.memory_dir
        else get_memory_dir("unitree")
    )
    return RunConfig(
        recipe_tag=f"unitree_{uuid4().hex[:12]}",
        output_dir=output_dir,
        prompt_vars={"task": task, "memory_dir": str(memory_dir)},
        task_desc={"task": task, "validation_scope": "unitree_mcp"},
    )


def _init_runtime(
    args: argparse.Namespace,
    output_dir: Path,
    dashboard_events: DashboardEventSink,
    components: set[str] | None,
) -> tuple[list[ProcessDaemon], dict[str, Any]]:
    return [], {}

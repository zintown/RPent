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

"""Natural-language CLI execution for robots controlled through MCP."""

from __future__ import annotations

import argparse
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from rpent.dashboard.events import DashboardEventSink
from rpent.memory import MemoryManager
from rpent.robots.prompt_bundle import PromptBundle
from rpent.robots.robot_spec import RobotSpec, RunConfig, TaskExecutionSpec
from rpent.session import EnvState
from rpent.tools import Toolkit
from rpent.utils.config import get_memory_dir, get_repo_root
from rpent.utils.daemon import ProcessDaemon


def mcp_robot_spec(
    name: str, prompts: PromptBundle, execution: TaskExecutionSpec
) -> RobotSpec:
    """Register a terminal task adapter that borrows configured MCP services."""
    return RobotSpec(
        name=name,
        is_real_robot=True,
        prompts=prompts,
        add_cli_args=_add_cli_args,
        parse_config=partial(_parse_config, name),
        init_runtime=_init_runtime,
        task_execution=execution,
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


def _parse_config(name: str, args: argparse.Namespace) -> RunConfig:
    task = (args.task or "").strip()
    if not task:
        raise ValueError("--task must contain a task description")
    if args.memory_profile != "local":
        raise ValueError(f"{name.capitalize()} runs require --memory-profile local")
    if not any(
        server.enabled and server.enabled_tools != []
        for server in getattr(args, "mcp_servers", {}).values()
    ):
        raise ValueError(f"{name.capitalize()} requires enabled tools in --mcp-config")

    tag = f"{name}_{uuid4().hex[:12]}"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(
        args.output_dir or get_repo_root() / "logs" / f"{timestamp}_{name}"
    )
    memory_dir = (
        Path(args.memory_dir).expanduser().resolve()
        if args.memory_dir
        else get_memory_dir(name)
    )
    return RunConfig(
        recipe_tag=tag,
        output_dir=output_dir,
        prompt_vars={"task": task, "memory_dir": str(memory_dir)},
        task_desc={
            "task": task,
            "validation_scope": "mcp" if name == "mcp" else f"{name}_mcp",
        },
    )


def _init_runtime(
    args: argparse.Namespace,
    output_dir: Path,
    dashboard_events: DashboardEventSink,
    components: set[str] | None,
) -> tuple[list[ProcessDaemon], dict[str, Any]]:
    return [], {}

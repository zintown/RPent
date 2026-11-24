"""Expose remote navigation as a verified task tool."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from robots.remote_navigation.navigation import (
    Navigation,
    NavigationServices,
    read_evidence,
)
from robots.remote_navigation.providers import Goal
from rpent.dashboard.events import DashboardEventSink
from rpent.mcp.task.config import TaskDeployment, load_deployment
from rpent.memory import MemoryManager
from rpent.prompt.utils import PromptNode
from rpent.robots.mcp_runtime import mcp_robot_spec
from rpent.robots.prompt_bundle import PromptBundle
from rpent.robots.robot_spec import RobotSpec, RunConfig, TaskExecutionSpec
from rpent.session import EnvState
from rpent.tools import Toolkit, ToolResult, iter_tools, tool
from rpent.tools.mcp_client import McpServiceError
from rpent.utils.daemon import ProcessDaemon


class NavigationToolkit(Toolkit):
    """Expose verified navigation operations using the task's MCP registry."""

    def __init__(
        self,
        services: NavigationServices,
        config: RunConfig,
        dashboard_events: DashboardEventSink,
    ) -> None:
        super().__init__(
            dashboard_events=dashboard_events,
            memory=MemoryManager(config.prompt_vars["memory_dir"]),
            state=EnvState(config.output_dir),
            require_finish=True,
        )
        self.services = services
        self.navigation = Navigation(
            services, self.mcp_registry, config.output_dir, self.raise_if_cancelled
        )
        self.add_tools(replace(tool, capture_state=False) for tool in iter_tools(self))

    @tool(readonly=True)
    def get_navigation_status(self) -> ToolResult:
        """Check if the remote map and simulator are ready for navigation."""
        try:
            reference = self.services.ready_map(
                self.mcp_registry, self.raise_if_cancelled
            )
        except McpServiceError as exc:
            return ToolResult(data={"ready": False, "reason": str(exc)})
        return ToolResult(data={"ready": True, **reference.model_dump()})

    @tool(readonly=True)
    def get_pose(self) -> ToolResult:
        """Read the robot's current map position and heading in meters and radians."""
        try:
            providers = self.services.providers(
                self.mcp_registry,
                "localization",
                check_cancelled=self.raise_if_cancelled,
            )
            pose = providers["localization"].pose()
        except McpServiceError as exc:
            return ToolResult(data={"error": str(exc)})
        return ToolResult(data=pose.model_dump())

    @tool
    def navigate(self, goals: Annotated[list[Goal], Field(min_length=1)]) -> ToolResult:
        """Follow a planned route and verify arrival at every requested goal.

        Args:
            goals: Destinations in order. Use names, waypoint IDs, or map x and y
                coordinates in meters with optional yaw in radians.
        """
        try:
            return ToolResult(data=self.navigation.navigate(goals))
        except McpServiceError as exc:
            return ToolResult(data={"error": str(exc)})


def _system(_variables: Mapping[str, object] | None = None) -> PromptNode:
    return """Execute the task on the configured robot.
Use get_navigation_status for readiness questions and get_pose for position questions.
Pass the requested destinations in order to navigate. It plans the route and waits
for arrival evidence. Preserve measured coordinates when the task asks to return
to the departure position. If an action fails or requires attention, report the
reason and any remaining work. Every task ends with finish. Report success when
the evidence confirms the requested outcome and include requested observations."""


def _user(_variables: Mapping[str, object] | None = None) -> PromptNode:
    return {"TASK": "{{task}}"}


def _configure_deployment(deployment: TaskDeployment) -> TaskDeployment:
    services = NavigationServices.from_deployment(deployment)
    return replace(
        deployment,
        execution=replace(
            deployment.execution,
            capability_checks={
                **deployment.execution.capability_checks,
                "map_navigation": services.check_readiness,
            },
        ),
    )


def _validate_args(args: Namespace) -> None:
    if args.mcp_config is None:
        raise ValueError("remote_navigation requires a deployment file in --mcp-config")
    deployment = load_deployment(args.mcp_config)
    if deployment.robot.adapter != "remote_navigation":
        raise ValueError(
            "remote_navigation requires [robot].adapter = 'remote_navigation'"
        )
    args.navigation_services = NavigationServices.from_deployment(deployment)
    args.mcp_servers = deployment.servers


def _init_runtime(
    args: Namespace,
    _output: Path,
    _events: DashboardEventSink,
    _components: set[str] | None,
) -> tuple[list[ProcessDaemon], dict[str, Any]]:
    return [], {"navigation_services": args.navigation_services}


def get_robot_spec() -> RobotSpec:
    """Register the remote navigation adapter for terminal and DSH tasks."""
    return replace(
        mcp_robot_spec(
            "remote_navigation",
            PromptBundle(system=_system, user=_user),
            TaskExecutionSpec(
                read_evidence=read_evidence,
                configure_deployment=_configure_deployment,
            ),
        ),
        validate_args=_validate_args,
        init_runtime=_init_runtime,
    )


def get_toolkit(
    *,
    runtime_kwargs: dict[str, Any],
    dashboard_events: DashboardEventSink,
    config: RunConfig,
) -> Toolkit:
    """Build the task's navigation toolkit from its resolved deployment."""
    return NavigationToolkit(
        runtime_kwargs["navigation_services"], config, dashboard_events
    )

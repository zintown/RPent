"""Adapt downstream MCP tools to map navigation operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, TypeVar

from jsonschema import Draft202012Validator
from mcp.types import TextContent
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StringConstraints,
    ValidationError,
)

from rpent.tools.mcp_client import McpClient, McpServiceError

Identifier = Annotated[str, Field(min_length=1)]
NamedGoal = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PathPoint = Annotated[list[FiniteFloat], Field(min_length=2, max_length=2)]


class _Response(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)


class MapReference(_Response):
    """Identity of the map used by a provider."""

    map_id: Identifier
    map_version: Identifier


class Pose(_Response):
    """Robot pose in map meters and radians."""

    x: FiniteFloat
    y: FiniteFloat
    yaw: FiniteFloat
    fallen: bool


class CoordinateGoal(BaseModel):
    """A point and optional final heading on the current map."""

    model_config = ConfigDict(extra="forbid", strict=True)

    x: FiniteFloat
    y: FiniteFloat
    yaw: FiniteFloat | None = None


Goal = NamedGoal | CoordinateGoal


@dataclass(frozen=True)
class RouteLeg:
    """One leg of a map route, ending at an optional heading."""

    points: list[PathPoint]
    final_yaw: float | None = None


@dataclass(frozen=True)
class Route:
    """A route in map meters and radians with an optional provider payload."""

    map: MapReference
    legs: list[RouteLeg]
    astar_plan: dict[str, Any] | None = None


class LegResult(_Response):
    """Arrival evidence for one leg."""

    reached: bool
    position_error: FiniteFloat | None = None
    yaw_error: FiniteFloat | None = None


class WalkResult(_Response):
    """The outcome reported by the execution service."""

    ok: bool
    legs: list[LegResult] = Field(default_factory=list)
    error: str | None = None


class WalkStatus(_Response):
    """State and evidence for the current or most recent action."""

    walk_id: Identifier
    state: Literal["running", "finished"]
    result: WalkResult | None = None
    pose: Pose | None = None
    fell: bool = False


class _SimulationStatus(_Response):
    simulator_running: bool
    other_controllers: list[Any]
    pose: Pose | None = None
    walk: dict[str, Any] | None = None


class _StandPose(_Response):
    yaw: FiniteFloat | None = None


class _GoalSite(_Response):
    stand_pose: _StandPose | None = None


class _PlanLeg(_Response):
    path: list[PathPoint] = Field(min_length=1)
    goal: _GoalSite


class _Units(_Response):
    length: Literal["meter"]
    angle: Literal["radian"]


class _Plan(_Response):
    frame: Literal["map"]
    units: _Units
    legs: list[_PlanLeg] = Field(min_length=1)


class _Submission(_Response):
    walk_id: Identifier


class RequestRejected(McpServiceError):
    """The service explicitly rejected the request."""


_Model = TypeVar("_Model", bound=BaseModel)


class McpProvider:
    """Use a discovered service and validate its structured tool results."""

    role_tools: ClassVar[dict[str, tuple[str, ...]]]

    def __init__(
        self, client: McpClient, check_cancelled: Callable[[], None] | None
    ) -> None:
        self.client = client
        self.check_cancelled = check_cancelled

    def call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        timeout_s: float | None = None,
        cleanup: bool = False,
    ) -> dict[str, Any]:
        label = f"{self.client.name}.{name}"
        validator = Draft202012Validator(self.client.tools[name].inputSchema)
        problem = next(validator.iter_errors(args), None)
        if problem is not None:
            raise RequestRejected(
                f"{label} input does not match its schema: {problem.message}"
            )
        if not cleanup and self.check_cancelled is not None:
            self.check_cancelled()
        result = self.client.call_tool(
            name,
            args,
            timeout_s=timeout_s,
            check_cancelled=None if cleanup else self.check_cancelled,
        )
        if result.isError:
            message = next(
                (
                    block.text
                    for block in result.content
                    if isinstance(block, TextContent)
                ),
                "MCP tool returned an error",
            )
            raise McpServiceError(f"{label}: {message}")
        data = result.structuredContent
        if not isinstance(data, dict):
            raise McpServiceError(f"{label} did not return structured content")
        if data.get("ok") is False:
            raise RequestRejected(f"{label}: {data.get('error') or 'request rejected'}")
        if data.get("ok") is not True:
            raise McpServiceError(f"{label} did not return a boolean ok field")
        return data

    def validate(self, model: type[_Model], data: dict[str, Any]) -> _Model:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise McpServiceError(
                f"{self.client.name} invalid response: {exc}"
            ) from exc


class RobotAstar(McpProvider):
    """Plan routes using the remote robot-astar contract."""

    role_tools = {"planner": ("service_health", "plan_route")}

    def ready_map(self) -> MapReference:
        return self.validate(MapReference, self.call("service_health", {}))

    def plan(self, pose: Pose, goals: list[Goal]) -> Route:
        data = self.call(
            "plan_route",
            {
                "start_x": pose.x,
                "start_y": pose.y,
                "start_yaw": pose.yaw,
                "goals": [
                    goal.model_dump(exclude_none=True)
                    if isinstance(goal, CoordinateGoal)
                    else goal
                    for goal in goals
                ],
            },
        )
        plan = self.validate(_Plan, data)
        return Route(
            self.validate(MapReference, data),
            [
                RouteLeg(
                    leg.path,
                    leg.goal.stand_pose.yaw
                    if leg.goal.stand_pose is not None
                    else None,
                )
                for leg in plan.legs
            ],
            astar_plan=data,
        )


class RobotSim(McpProvider):
    """Locate and execute routes using the remote robot-sim contract."""

    role_tools = {
        "localization": ("sim_status", "get_pose"),
        "executor": ("sim_status", "walk_start", "walk_status", "walk_stop"),
    }

    def ready_map(self) -> MapReference:
        data = self.call("sim_status", {})
        status = self.validate(_SimulationStatus, data)
        if not status.simulator_running:
            raise McpServiceError(f"{self.client.name} simulator is stopped")
        if status.pose is None:
            raise McpServiceError(f"{self.client.name} simulator pose is unavailable")
        if status.pose.fallen:
            raise McpServiceError(f"{self.client.name} robot has fallen")
        if status.other_controllers:
            raise McpServiceError(f"{self.client.name} has another active controller")
        if status.walk and status.walk.get("state") not in ("idle", "finished"):
            raise McpServiceError(f"{self.client.name} has an unfinished walk")
        return self.validate(MapReference, data)

    def pose(self) -> Pose:
        return self.validate(Pose, self.call("get_pose", {}))

    def start(self, route: Route) -> str:
        if route.astar_plan is not None:
            args = {"plan": route.astar_plan}
        else:
            if len(route.legs) != 1:
                raise RequestRejected("robot-sim points require a single route leg")
            if self.ready_map() != route.map:
                raise RequestRejected("simulator map changed after planning")
            [leg] = route.legs
            args = {"points": leg.points}
            if leg.final_yaw is not None:
                args["final_yaw"] = leg.final_yaw
        return self.validate(_Submission, self.call("walk_start", args)).walk_id

    def status(
        self, walk_id: str, *, timeout_s: float, cleanup: bool = False
    ) -> WalkStatus:
        status = self.validate(
            WalkStatus,
            self.call("walk_status", {}, timeout_s=timeout_s, cleanup=cleanup),
        )
        if status.walk_id != walk_id:
            raise McpServiceError(
                f"{self.client.name} returned walk {status.walk_id!r}, expected {walk_id!r}"
            )
        return status

    def stop(self, *, timeout_s: float) -> None:
        self.call("walk_stop", {}, timeout_s=timeout_s, cleanup=True)


SERVICE_ADAPTERS = {"robot_astar": RobotAstar, "robot_sim": RobotSim}

"""Coordinate map navigation and record evidence for remote actions."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep, time_ns
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from robots.remote_navigation.providers import (
    SERVICE_ADAPTERS,
    Goal,
    MapReference,
    McpProvider,
    RequestRejected,
    WalkStatus,
)
from rpent.evaluation import write_json_atomic
from rpent.mcp.task.config import TaskDeployment
from rpent.robots.robot_spec import TaskEvidence
from rpent.tools.mcp_client import McpClient, McpServerConfig, McpServiceError
from rpent.tools.toolkit import ToolCancelled

ROLES = ("localization", "planner", "executor")
WALK_TIMEOUT_S = 600.0
POLL_INTERVAL_S = 5.0
STOP_TIMEOUT_S = 12.0
UNCERTAIN = {"submitting", "running", "requires_attention"}


@dataclass(frozen=True)
class NavigationServices:
    """Resolve the providers selected for one map navigation capability."""

    bindings: dict[str, str]
    configs: dict[str, McpServerConfig]
    adapters: dict[str, type[McpProvider]]

    @classmethod
    def from_deployment(cls, deployment: TaskDeployment) -> NavigationServices:
        capability = deployment.capabilities.get("map_navigation")
        if capability is None or capability.bindings.keys() != set(ROLES):
            raise ValueError(
                "map_navigation requires localization, planner and executor bindings"
            )
        if any(
            name != "map_navigation" and cap.bindings
            for name, cap in deployment.capabilities.items()
        ):
            raise ValueError("remote_navigation supports bindings for map_navigation")

        configs = {}
        adapters = {}
        for role, name in capability.bindings.items():
            config = deployment.servers.get(name)
            adapter_name = deployment.service_adapters.get(name)
            adapter = SERVICE_ADAPTERS.get(adapter_name)
            if config is None or not config.enabled or config.enabled_tools == []:
                raise ValueError(
                    f"map_navigation {role} requires an enabled service {name!r}"
                )
            if adapter is None or role not in adapter.role_tools:
                raise ValueError(f"service {name!r} has no adapter for {role}")
            if (
                adapter_name == "robot_sim"
                and deployment.robot.environment != "simulation"
            ):
                raise ValueError("robot_sim requires a simulation deployment")
            if config.enabled_tools is not None:
                missing = set(adapter.role_tools[role]) - set(config.enabled_tools)
                if missing:
                    raise ValueError(
                        f"service {name!r} must enable tools {sorted(missing)}"
                    )
            configs[name] = config
            adapters[name] = adapter
        return cls(capability.bindings, configs, adapters)

    @contextmanager
    def connect(
        self,
        *roles: str,
        check_cancelled: Callable[[], None] | None = None,
        probe: bool = False,
    ) -> Iterator[dict[str, McpProvider]]:
        """Connect each selected service once and require its bound tools."""
        with ExitStack() as stack:
            providers: dict[str, McpProvider] = {}
            for role in roles:
                name = self.bindings[role]
                if name not in providers:
                    if check_cancelled is not None:
                        check_cancelled()
                    config = self.configs[name]
                    if probe:
                        config = config.model_copy(
                            update={
                                "startup_timeout_sec": min(
                                    config.startup_timeout_sec, 5
                                ),
                                "tool_timeout_sec": min(config.tool_timeout_sec, 5),
                            }
                        )
                    client = stack.enter_context(closing(McpClient(name, config)))
                    selected = client.select_tools(config)
                    adapter = self.adapters[name]
                    required_tools = {
                        tool
                        for bound_role in roles
                        if self.bindings[bound_role] == name
                        for tool in adapter.role_tools[bound_role]
                    }
                    missing = required_tools - selected.keys()
                    if missing:
                        raise McpServiceError(
                            f"{name} does not export tools {sorted(missing)}"
                        )
                    for tool in required_tools:
                        try:
                            Draft202012Validator.check_schema(
                                selected[tool].inputSchema
                            )
                        except SchemaError as exc:
                            raise McpServiceError(
                                f"{name}.{tool} has an invalid input schema"
                            ) from exc
                    providers[name] = adapter(client, check_cancelled)
            yield {role: providers[self.bindings[role]] for role in roles}

    def ready_map(
        self, check_cancelled: Callable[[], None] | None = None
    ) -> MapReference:
        """Check live readiness without starting a simulation or an action."""
        with self.connect(
            *ROLES, probe=True, check_cancelled=check_cancelled
        ) as providers:
            return _ready_map(providers)

    def check_capability(self, _name: str) -> str | None:
        try:
            self.ready_map()
        except McpServiceError as exc:
            return str(exc)
        return None


def _ready_map(providers: dict[str, McpProvider]) -> MapReference:
    maps = {
        provider.client.name: provider.ready_map()
        for provider in dict.fromkeys(providers.values())
    }
    reference = maps[providers["planner"].client.name]
    if any(current != reference for current in maps.values()):
        versions = ", ".join(
            f"{name}={current.map_id}/{current.map_version}"
            for name, current in maps.items()
        )
        raise McpServiceError(f"navigation map mismatch ({versions})")
    return reference


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise McpServiceError(
            "walk did not reach a terminal state within the time budget"
        )
    return remaining


def _wait(
    executor: McpProvider, walk_id: str, deadline: float, *, cleanup: bool = False
) -> WalkStatus:
    while True:
        status = executor.status(
            walk_id, timeout_s=_remaining(deadline), cleanup=cleanup
        )
        if status.state == "finished":
            return status
        interval = 0.5 if cleanup else POLL_INTERVAL_S
        sleep(min(interval, _remaining(deadline)))


def _stop_owned(executor: McpProvider, walk_id: str) -> WalkStatus:
    deadline = monotonic() + STOP_TIMEOUT_S
    status = executor.status(
        walk_id, timeout_s=min(4, _remaining(deadline)), cleanup=True
    )
    if status.state == "finished":
        return status
    try:
        executor.stop(timeout_s=min(4, _remaining(deadline)))
    except McpServiceError as exc:
        # The action may have finished during the stop request or its reply may be lost.
        try:
            return _wait(executor, walk_id, deadline, cleanup=True)
        except McpServiceError as verification:
            raise McpServiceError(f"{exc}; {verification}") from verification
    return _wait(executor, walk_id, deadline, cleanup=True)


def _store(path: Path, record: dict[str, Any], **fields: Any) -> None:
    record.update(fields)
    write_json_atomic(path, record)


def _records(output: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(output.glob("navigation-*.json"))
    ]


class Navigation:
    """Run planned routes and verify the outcome of each owned walk."""

    def __init__(
        self,
        services: NavigationServices,
        output: Path,
        check_cancelled: Callable[[], None],
    ) -> None:
        self.services = services
        self.output = output
        self.check_cancelled = check_cancelled

    def navigate(self, goals: list[Goal]) -> dict[str, Any]:
        if any(record["state"] in UNCERTAIN for record in _records(self.output)):
            raise McpServiceError(
                "Verify the prior walk before requesting another action"
            )
        path = self.output / f"navigation-{time_ns()}-{uuid4().hex}.json"
        record = {"state": "planning", "goal_count": len(goals)}
        try:
            with self.services.connect(
                *ROLES, check_cancelled=self.check_cancelled
            ) as providers:
                self._execute(providers, goals, path, record)
        except McpServiceError as exc:
            if record["state"] == "planning":
                _store(path, record, state="failed", error=str(exc))
            return {**record, "ok": False, "error": str(exc)}
        return {**record, "ok": record["state"] == "succeeded"}

    def _execute(
        self,
        providers: dict[str, McpProvider],
        goals: list[Goal],
        path: Path,
        record: dict[str, Any],
    ) -> None:
        reference = _ready_map(providers)
        pose = providers["localization"].pose()
        if pose.fallen:
            raise McpServiceError("The robot has fallen")
        route = providers["planner"].plan(pose, goals)
        if route.map != reference:
            raise McpServiceError("The planner map changed before route submission")
        if len(route.legs) != len(goals):
            raise McpServiceError(
                "The planner did not return a leg for every requested goal"
            )
        executor = providers["executor"]
        self.check_cancelled()
        _store(path, record, state="submitting", **reference.model_dump())
        try:
            walk_id = executor.start(route)
        except RequestRejected as exc:
            _store(path, record, state="failed", error=str(exc))
            raise
        except McpServiceError as exc:
            _store(
                path,
                record,
                state="requires_attention",
                error=str(exc),
                stop_error="Walk submission outcome could not be verified",
            )
            raise

        try:
            _store(path, record, state="running", walk_id=walk_id)
            status = _wait(executor, walk_id, monotonic() + WALK_TIMEOUT_S)
        except BaseException as exc:
            self._interrupt(executor, walk_id, path, record, exc)
            raise

        result = status.result
        legs = result.legs if result else []
        reached = sum(leg.reached for leg in legs[: len(route.legs)])
        succeeded = bool(
            result
            and result.ok
            and len(legs) == len(route.legs)
            and reached == len(route.legs)
            and not status.fell
            and not (status.pose and status.pose.fallen)
        )
        _store(
            path,
            record,
            state="succeeded" if succeeded else "failed",
            walk_state=status.state,
            completed_navigation_goals=reached,
            **status.model_dump(exclude={"state"}, exclude_none=True),
            error=None
            if succeeded
            else (
                (result.error if result else None)
                or "Walk ended without reaching every requested goal"
            ),
        )

    def _interrupt(
        self,
        executor: McpProvider,
        walk_id: str,
        path: Path,
        record: dict[str, Any],
        cause: BaseException,
    ) -> None:
        try:
            stopped = _stop_owned(executor, walk_id)
        except McpServiceError as exc:
            _store(
                path,
                record,
                state="requires_attention",
                error=str(cause),
                stop_error=str(exc),
            )
        else:
            _store(
                path,
                record,
                state="stopped"
                if isinstance(cause, (KeyboardInterrupt, ToolCancelled))
                else "failed",
                walk_state=stopped.state,
                error=str(cause),
                **stopped.model_dump(exclude={"state"}, exclude_none=True),
            )


def read_evidence(output: Path, _transcript: dict[str, Any]) -> TaskEvidence:
    """Report navigation evidence to the task MCP after the worker exits."""
    records = _records(output)
    if not records:
        return TaskEvidence()
    uncertain = next(
        (record for record in records if record["state"] in UNCERTAIN), None
    )
    failed = next((record for record in records if record["state"] == "failed"), None)
    return TaskEvidence(
        data={
            "navigation": {
                **records[-1],
                "actions": records,
                "completed_navigation_goals": sum(
                    record.get("completed_navigation_goals", 0) for record in records
                ),
            }
        },
        requires_attention=uncertain is not None,
        error=(uncertain or failed or {}).get("error"),
        stop_error=(
            uncertain.get("stop_error") or "The last remote walk could not be verified"
            if uncertain
            else None
        ),
    )

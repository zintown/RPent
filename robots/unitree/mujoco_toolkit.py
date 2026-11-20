# Copyright 2026 The RPent Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Agent tools for map-frame planning and external MuJoCo route following."""

from __future__ import annotations

import copy
import math
import threading
import time
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from robots.unitree.http_client import ServiceError, map_identity
from robots.unitree.navigation_client import (
    CoordinateGoal,
    NavigationClient,
    NavigationGoal,
)
from robots.unitree.observation_client import ObservationClient
from robots.unitree.simulation_client import SimulationClient, validate_walk_arrival
from robots.unitree.vln_client import VlnClient
from robots.unitree.vln_navigation import VlnNavigation
from rpent.dashboard.events import DashboardEventSink
from rpent.evaluation import write_json_atomic
from rpent.memory import MemoryManager
from rpent.session import EnvState
from rpent.tools import Toolkit, ToolResult, tool
from rpent.tools.toolkit import ToolCancelled
from rpent.utils.logging import get_logger

logger = get_logger("unitree_navigation")


class UnitreeMujocoToolkit(Toolkit):
    """Plan routes and await single-goal walks on an exclusively used sim service.

    The service may omit walk_id in status replies. In that case ownership is
    local to this toolkit, so sharing one simulation endpoint between runners
    is unsupported. No simulator restart or other-controller stop is exposed.
    """

    def __init__(
        self,
        *,
        nav: NavigationClient,
        sim: SimulationClient,
        dashboard_events: DashboardEventSink,
        memory: MemoryManager,
        state_output_dir: Path,
        navigation_timeout_s: float = 300.0,
        poll_interval_s: float = 5.0,
        vln: VlnClient | None = None,
        observation: ObservationClient | None = None,
        vln_timeout_s: float = 120.0,
    ) -> None:
        for value in (navigation_timeout_s, poll_interval_s):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("navigation waits must be finite and positive")
        self._nav, self._sim = nav, sim
        self._timeout_s, self._poll_s = navigation_timeout_s, poll_interval_s
        self._artifacts = state_output_dir / "navigation"
        self._plans: dict[str, dict[str, Any]] = {}
        self._completed_plans: set[str] = set()
        self._failed_previews: set[str] = set()
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._active_walk_id: str | None = None
        self._submission_uncertain = False
        self._stop_requested = False
        self._motion_attempted = False
        self._completed = 0
        self._last_pose: dict[str, Any] | None = None
        self._last_walk: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._stop_error: str | None = None
        if (vln is None) != (observation is None):
            raise ValueError("VLN requires both model and observation clients")
        if not math.isfinite(vln_timeout_s) or vln_timeout_s <= 0:
            raise ValueError("VLN timeout must be finite and positive")
        self._vln_timeout_s = vln_timeout_s
        self._unverified_vln = False
        self._observation = observation
        self._vln = (
            None
            if vln is None
            else VlnNavigation(
                sim=sim,
                observation=observation,
                model=vln,
                artifacts=self._artifacts / "vln",
            )
        )
        super().__init__(
            dashboard_events=dashboard_events,
            memory=memory,
            state=EnvState(state_output_dir),
        )
        self.add_tools(
            (
                self.list_navigation_goals,
                self.get_navigation_pose,
                self.plan_navigation,
                self.preview_navigation,
                self.follow_navigation,
                self.get_navigation_status,
                self.stop_navigation,
            )
        )
        self.add_tool(self.finish_navigation, replace=True)
        if self._vln is not None:
            self.add_tools(
                (self.run_vln_navigation, self.get_vln_status, self.stop_vln_navigation)
            )
        with self.state.record_step(state=self._state_summary()):
            pass
        self._publish_step(self.state.get())

    def _state_summary(self) -> dict[str, Any]:
        with self._lock:
            summary = {
                "completed_navigation_goals": self._completed,
                "active_walk_id": self._active_walk_id,
                "submission_uncertain": self._submission_uncertain,
                "pose": copy.deepcopy(self._last_pose),
                "error": self._last_error,
                "stop_error": self._stop_error,
            }
            if self._vln is not None:
                vln = self._vln.snapshot()
                summary.update(
                    vln=vln,
                    active_control_id=vln["active_control_id"],
                    submission_uncertain=self._submission_uncertain
                    or vln["submission_uncertain"],
                    stop_error=self._stop_error or vln["stop_error"],
                )
            return summary

    def _ready(self) -> tuple[str, str]:
        nav = self._nav.health()
        sim = self._sim.health()
        identity = map_identity(nav)
        if map_identity(sim) != identity:
            raise ServiceError(
                "planning and simulation map_id/map_version do not match"
            )
        if not sim["simulator_running"]:
            raise ServiceError("simulator is not running; start it on the server first")
        if sim.get("other_controllers"):
            raise ServiceError(
                "other controllers are active; no navigation was submitted"
            )
        return identity

    def _pose(self) -> dict[str, Any]:
        pose = self._sim.get_pose()
        with self._lock:
            self._last_pose = pose
        return pose

    def _plan(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            if plan_id not in self._plans:
                raise ValueError("unknown plan_id; call plan_navigation first")
            return copy.deepcopy(self._plans[plan_id])

    @tool(readonly=True)
    def list_navigation_goals(self) -> ToolResult:
        """Read current waypoint names, ids, and map-frame standing poses."""
        return ToolResult(data=self._nav.list_waypoints())

    @tool(readonly=True)
    def get_navigation_pose(self) -> ToolResult:
        """Read the simulator's current map-frame position and yaw in radians."""
        return ToolResult(data=self._pose())

    @tool(readonly=True)
    def plan_navigation(
        self,
        goals: list[NavigationGoal],
        step: float | None = None,
    ) -> ToolResult:
        """Plan from a fresh simulator pose and save the unmodified route.

        Args:
            goals: Ordered waypoint names/ids or map-coordinate objects. Use one goal per executable plan.
            step: Optional dense-path spacing in meters; not a motion speed.
        """
        identity = self._ready()
        pose = self._pose()
        if pose["fallen"]:
            raise ServiceError("robot has fallen; simulator recovery is required")
        plan = self._nav.plan(pose, goals, step=step)
        if map_identity(plan) != identity:
            raise ServiceError("map changed during planning; query the services again")
        plan_id = "plan_" + uuid4().hex[:12]
        destination = self._artifacts / "plans" / f"{plan_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(destination, plan)
        with self._lock:
            self._plans[plan_id] = plan
        return ToolResult(
            data={
                "ok": True,
                "plan_id": plan_id,
                "map_id": identity[0],
                "map_version": identity[1],
                "goals": [leg["goal"] for leg in plan["legs"]],
                "total_length": plan["total_length"],
                "warnings": plan.get("warnings", []),
                "plan_file": str(destination),
                "executable": len(plan["legs"]) == 1,
            }
        )

    @tool(readonly=True)
    def preview_navigation(self, plan_id: str) -> ToolResult:
        """Preview a saved route in independent simulation without moving the live dog.

        Args:
            plan_id: Identifier returned by plan_navigation.
        """
        plan = self._plan(plan_id)
        if self._ready() != map_identity(plan):
            raise ServiceError("saved plan uses a different map; replan")
        with self._lock:
            self._failed_previews.add(plan_id)
        result = self._sim.preview(plan, timeout_s=self._timeout_s)
        destination = self._artifacts / "previews" / f"{plan_id}_{uuid4().hex[:8]}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(destination, result)
        preview_result = result.get("result")
        # The preview API guarantees top-level ok, not a nested result.ok field.
        # Still reject an explicitly reported nested failure when provided.
        if isinstance(preview_result, dict) and (
            preview_result.get("ok") is False
            or any(
                isinstance(leg, dict) and leg.get("reached") is False
                for leg in preview_result.get("legs", [])
            )
        ):
            raise ServiceError(f"preview did not confirm arrival; see {destination}")
        with self._lock:
            self._failed_previews.discard(plan_id)
        return ToolResult(
            data={
                "ok": True,
                "plan_id": plan_id,
                "result": result.get("result"),
                "image": result.get("image"),
                "report_file": str(destination),
            }
        )

    def _observe_walk(self) -> dict[str, Any]:
        with self._lock:
            expected_id = self._active_walk_id
        status = self._sim.get_walk_status()
        with self._lock:
            observed_id = status.get("walk_id")
            if (
                expected_id is not None
                and observed_id is not None
                and observed_id != expected_id
            ):
                # Never stop a different client's task when ownership has changed.
                self._active_walk_id = None
                raise ServiceError(
                    "simulation walk_id changed; task ownership was lost"
                )
            self._last_walk = status
            if isinstance(status.get("pose"), dict):
                self._last_pose = status["pose"]
            if status["state"] == "finished":
                self._active_walk_id = None
        return status

    def _request_stop(self) -> bool:
        with self._lock:
            if self._active_walk_id is None or self._stop_requested:
                return True
        try:
            self._observe_walk()
        except ServiceError as exc:
            if self._active_walk_id is None:
                # An observed id change invalidates our ownership.
                self._stop_error = f"stop was not confirmed: {exc}"
                return False
            # A failed observation does not relinquish an accepted owned task.
            # The endpoint is exclusively used by this runner; still send stop.
            logger.warning("Cannot query owned walk before stopping: %s", exc)
        with self._lock:
            if self._active_walk_id is None:
                self._stop_error = None
                return True
            if self._stop_requested:
                return True
            self._stop_requested = True
        try:
            self._sim.stop_walk()
        except ServiceError as exc:
            with self._lock:
                self._stop_error = f"stop was not confirmed: {exc}"
            logger.error("%s", self._stop_error)
            return False
        return True

    def _stop_and_confirm(self) -> bool:
        requested = self._request_stop()
        if self._active_walk_id is None:
            return requested
        deadline = time.monotonic() + 10.0
        while self._active_walk_id is not None and time.monotonic() < deadline:
            try:
                self._observe_walk()
            except ServiceError as exc:
                with self._lock:
                    self._stop_error = f"stop was not confirmed: {exc}"
                return False
            if self._active_walk_id is not None:
                time.sleep(0.05)
        if self._active_walk_id is not None:
            with self._lock:
                self._stop_error = "stop was not confirmed before cleanup timeout"
            return False
        self._stop_error = None
        return True

    @tool
    def follow_navigation(self, plan_id: str) -> ToolResult:
        """Execute one saved single-goal route and wait for settled arrival.

        Args:
            plan_id: An unexecuted single-goal plan from plan_navigation. Plan and follow successive goals separately.
        """
        self._motion_attempted = True
        self._last_error = None
        if self._vln is not None and self._vln.active:
            raise ServiceError(
                "VLN control is active or uncertain; release it before following an A* route"
            )
        plan = self._plan(plan_id)
        if len(plan["legs"]) != 1:
            raise ValueError("follow one goal at a time to preserve ordered arrival")
        with self._lock:
            if plan_id in self._completed_plans:
                raise ValueError(
                    "plan already completed; read the current pose and replan"
                )
            if plan_id in self._failed_previews:
                raise ServiceError("preview failed; do not follow this plan")
            if self._active_walk_id is not None or self._submission_uncertain:
                raise ServiceError(
                    "a prior walk is active or its submission is uncertain"
                )
        if self._ready() != map_identity(plan):
            raise ServiceError("saved plan uses a different map; replan")
        health = self._sim.health()
        if (health.get("walk") or {}).get("state") == "running":
            raise ServiceError("simulation service already has a running walk")
        pose = self._pose()
        if pose["fallen"]:
            raise ServiceError("robot has fallen")
        if (
            math.hypot(pose["x"] - plan["start"]["x"], pose["y"] - plan["start"]["y"])
            > 0.3
        ):
            raise ServiceError("robot moved away from the saved start; replan")
        destination = self._artifacts / "walks" / f"{plan_id}_{uuid4().hex[:8]}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout_s
        try:
            self.raise_if_cancelled()
            # A timeout or malformed acceptance may follow a remotely started walk.
            self._submission_uncertain = True
            accepted = self._sim.start_walk(plan)
            with self._lock:
                self._active_walk_id = accepted["walk_id"]
                self._submission_uncertain = False
                self._stop_requested = False
                self._stop_error = None
                self._last_walk = None
            while True:
                self.raise_if_cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("navigation timed out; the walk is not retried")
                status = self._observe_walk()
                self.raise_if_cancelled()
                if status["state"] == "finished":
                    validate_walk_arrival(status, plan)
                    with self._lock:
                        self._completed += 1
                        self._completed_plans.add(plan_id)
                    return ToolResult(
                        data={
                            "ok": True,
                            "navigation_completed": True,
                            "plan_id": plan_id,
                            "walk_id": accepted["walk_id"],
                            "state": "finished",
                            "result": status["result"],
                            "pose": status["pose"],
                            "report_file": str(destination),
                        }
                    )
                self._cancelled.wait(
                    min(self._poll_s, max(0, deadline - time.monotonic()))
                )
        except (ServiceError, TimeoutError, ToolCancelled) as exc:
            self._last_error = str(exc)
            raise
        finally:
            if self._active_walk_id is not None:
                self._stop_and_confirm()
            write_json_atomic(
                destination,
                {
                    "plan_id": plan_id,
                    **self._state_summary(),
                    "walk": self._last_walk,
                },
            )

    @tool
    def run_vln_navigation(
        self,
        instruction: str,
        expected_goal: CoordinateGoal | None = None,
        timeout_s: float | None = None,
    ) -> ToolResult:
        """Run image-based LightNav navigation, waiting internally for stopping and cleanup.

        Args:
            instruction: The complete visual navigation instruction for LightNav.
            expected_goal: Optional known map pose for independent arrival verification. Without it, model STOP yields verification_required, never verified success.
            timeout_s: Episode budget in seconds, at most the configured VLN timeout.
        """
        if self._vln is None:
            raise ServiceError(
                "VLN is not configured; set --vln-endpoint and --observation-endpoint"
            )
        if self._active_walk_id is not None or self._submission_uncertain:
            raise ServiceError("a route is active or uncertain; do not start VLN")
        budget = self._vln_timeout_s if timeout_s is None else timeout_s
        if not math.isfinite(budget) or not 0 < budget <= self._vln_timeout_s:
            raise ValueError(
                "VLN timeout must be positive and within the configured budget"
            )
        self._motion_attempted = True
        self._last_error = None
        try:
            result = self._vln.run(
                instruction,
                timeout_s=budget,
                expected_goal=None
                if expected_goal is None
                else expected_goal.model_dump(exclude_none=True),
                check_cancelled=self.raise_if_cancelled,
            )
            with self._lock:
                self._last_pose = result.get("pose")
                if result["goal_verified"]:
                    self._completed += 1
                else:
                    self._unverified_vln = True
            return ToolResult(data={"ok": True, **result})
        except (ServiceError, TimeoutError, ToolCancelled) as exc:
            self._last_error = str(exc)
            raise

    @tool(readonly=True)
    def get_vln_status(self) -> ToolResult:
        """Read the last VLN episode, inference progress, control lease and stop evidence."""
        if self._vln is None:
            raise ServiceError("VLN is not configured")
        return ToolResult(data=self._vln.snapshot())

    @tool
    def stop_vln_navigation(self) -> ToolResult:
        """Stop only this toolkit's VLN lease and require simulator stop confirmation."""
        if self._vln is None:
            return ToolResult(data={"ok": True, "stopped": False})
        was_active = self._vln.active
        self._vln.cancel()
        state = self._vln.snapshot()
        if state["active_control_id"] is not None or state["submission_uncertain"]:
            raise ServiceError(
                state["stop_error"] or "VLN stopping requires reconciliation"
            )
        return ToolResult(data={"ok": True, "stopped": was_active, **state})

    @tool(readonly=True)
    def get_navigation_status(self) -> ToolResult:
        """Read both services and any walk still owned by this toolkit."""
        nav, sim = self._nav.health(), self._sim.health()
        if self._active_walk_id is not None:
            self._observe_walk()
        pose = self._pose() if sim["simulator_running"] else None
        return ToolResult(
            data={
                "planning": nav,
                "simulation": sim,
                "pose": pose,
                "navigation": self._state_summary(),
            }
        )

    @tool
    def stop_navigation(self) -> ToolResult:
        """Stop only a walk owned by this toolkit and confirm it has finished."""
        if self._submission_uncertain:
            raise ServiceError(
                "submission is uncertain without a walk_id; verify the service before retrying"
            )
        if self._active_walk_id is None:
            return ToolResult(data={"ok": True, "stopped": False})
        if not self._stop_and_confirm():
            raise ServiceError(self._stop_error or "stop was not confirmed")
        return ToolResult(data={"ok": True, "stopped": True})

    @tool(name="finish", readonly=True)
    def finish_navigation(
        self, status: Literal["success", "failure", "stuck"], summary: str
    ) -> ToolResult:
        """Finish a query task or report verified arrival after navigation.

        Args:
            status: success, failure, or stuck.
            summary: Explain the result and any remaining goals.
        """
        if status == "success" and (
            self._unverified_vln
            or (
                self._vln is not None
                and (
                    self._vln.active
                    or self._vln.snapshot().get("state")
                    in {"failed", "cancelled", "verification_required"}
                )
            )
        ):
            return ToolResult(
                data={
                    "error": "VLN goal has not been independently verified or its control is still active"
                }
            )
        if (
            status == "success"
            and self._motion_attempted
            and (
                self._active_walk_id is not None
                or self._submission_uncertain
                or self._last_error is not None
                or self._stop_error is not None
                or self._completed == 0
            )
        ):
            return ToolResult(
                data={"error": "navigation has not completed successfully"}
            )
        return ToolResult(data={"_finish": True, "status": status, "summary": summary})

    def get_env_state(
        self, *, command: dict[str, Any], result: dict[str, Any], elapsed_s: float
    ) -> ToolResult:
        """Record navigation outcomes without inventing physical task success."""
        if result.get("error"):
            self._last_error = str(result["error"])
        with self.state.record_step(
            state=self._state_summary(),
            command=command,
            result=result,
            elapsed_s=elapsed_s,
            extras={"validation_scope": "mujoco_navigation"},
        ) as step:
            pass
        return ToolResult(data={**result, "step": step})

    def cancel_active_and_wait(self) -> None:
        """Request remote stopping before waiting for the local tool to drain."""
        self._cancelled.set()
        calls = self._scheduler.cancel()
        try:
            self._request_stop()
            if self._vln is not None:
                self._vln.cancel()
        finally:
            self._scheduler.wait(calls)
        if self._active_walk_id is not None:
            self._stop_and_confirm()

    def resume_calls(self) -> None:
        """Resume only after cancellation cleanup has drained the tools."""
        super().resume_calls()
        self._cancelled.clear()

    def close(self) -> None:
        """Drain tools and close owned clients, leaving both external services up."""
        try:
            super().close()
        finally:
            try:
                self._nav.close()
            finally:
                try:
                    self._sim.close()
                finally:
                    try:
                        if self._vln is not None:
                            self._vln.model.close()
                    finally:
                        if self._observation is not None:
                            self._observation.close()

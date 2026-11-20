# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

"""Execute physical Go2 requests and reconcile their stopping evidence."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from anyio import from_thread

from robots.unitree.http_client import ServiceError, finite_number
from robots.unitree.mcp.real.client import (
    RealRobotClient,
    RobotRequestRejected,
    relative_command,
)
from rpent.evaluation import write_json_atomic

_POLL_INTERVAL_S = 1.0
_STOP_BUDGET_S = 12.0


def _feedback(record: dict[str, Any], kind: str) -> dict[str, Any]:
    feedback = record["result"] if record["result_available"] else None
    if (
        record["outcome_known"]
        and isinstance(feedback, dict)
        and feedback.get("type") == kind
    ):
        return feedback
    return {}


def _motion_stopped(record: dict[str, Any]) -> bool:
    result = _feedback(record, "command_result").get("result")
    return (
        record["status"] in ("failed", "completed")
        and isinstance(result, dict)
        and result.get("final_state_available") is True
        and result.get("stop_ack") is True
    )


def _motion_resolved(record: dict[str, Any]) -> bool:
    return _motion_stopped(record) or (
        record["outcome_known"] and record["status"] in ("dry_run", "rejected")
    )


def _completed_move(record: dict[str, Any]) -> bool:
    feedback = _feedback(record, "command_result")
    return (
        record["status"] == "completed"
        and _motion_stopped(record)
        and feedback.get("status") == "completed"
        and feedback.get("completed") is True
        and feedback.get("dry_run") is False
        and type(feedback.get("exit_code")) is int
        and feedback["exit_code"] == 0
        and feedback.get("reason") == "completed"
        and feedback["result"].get("success") is True
        and feedback["result"].get("arrival_phase_started") is True
    )


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    feedback = record.get("result")
    if not isinstance(feedback, dict):
        return record
    return {
        **record,
        "result": {
            key: value
            for key, value in feedback.items()
            if key not in {"raw_result", "stdout", "stderr"}
        },
    }


def _failed(record: dict[str, Any], action: str) -> ServiceError:
    feedback = record.get("result")
    reason = record.get("error") or (
        feedback.get("reason") if isinstance(feedback, dict) else None
    )
    return ServiceError(
        f"{action} request {record['request_id']} not confirmed "
        f"({record['status']}): {reason or 'no final evidence'}"
    )


class RealRobotActions:
    """Serialize real robot actions and retain evidence when motion is uncertain."""

    def __init__(
        self,
        *,
        real: RealRobotClient,
        state_output_dir: Path,
        action_timeout_s: float,
    ) -> None:
        if not math.isfinite(action_timeout_s) or action_timeout_s <= 0:
            raise ValueError("action timeout must be finite and positive")
        self._action_lock = Lock()
        self._env = real
        self._timeout_s = action_timeout_s
        self._state_path = state_output_dir / "real" / "latest.json"
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection_started: float | None = None
        self._move_key: str | None = None
        self._move_id: str | None = None
        self._last_move_id: str | None = None
        self._last_move_status: str | None = None
        self._stop_id: str | None = None
        self._stop_confirmed: bool | None = None
        self._stop_error: str | None = None
        self._error: str | None = None
        self._completed = 0
        self._persist()

    def perform(
        self, action: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        """Allow one action at a time, retaining any error in the task evidence."""
        if not self._action_lock.acquire(blocking=False):
            raise ServiceError("a Go2 action is already in progress")
        try:
            return action(*args, **kwargs)
        except Exception as exc:
            self._error = str(exc)
            raise
        finally:
            try:
                self._persist()
            finally:
                self._action_lock.release()

    def _snapshot(self) -> dict[str, Any]:
        return {
            "backend": "real",
            "robot_index": self._env.robot_index,
            "completed_moves": self._completed,
            "active_request_id": self._move_id,
            "last_move_request_id": self._last_move_id,
            "last_move_status": self._last_move_status,
            "client_request_id": self._move_key,
            "submission_uncertain": self._move_key is not None
            and self._move_id is None,
            "stop_request_id": self._stop_id,
            "stop_confirmed": self._stop_confirmed,
            "stop_error": self._stop_error,
            "error": self._error,
        }

    def _persist(self) -> None:
        write_json_atomic(self._state_path, self._snapshot())

    def _ready(self, *, movement: bool = False) -> None:
        status = self._env.health()
        if (
            status["status"] != "online"
            or status.get("connected") is not True
            or status.get("heartbeat_healthy") is not True
        ):
            raise ServiceError(
                "assigned robot does not have a live connection and heartbeat"
            )
        connected = finite_number(status.get("connected_at_unix"), "connected_at_unix")
        if self._connection_started is None:
            self._connection_started = connected
        elif self._connection_started != connected:
            raise ServiceError(
                "robot index reconnected; verify its physical identity before acting"
            )
        if movement and status.get("controller_ready") is False:
            raise ServiceError("robot controller is not ready")

    def _allow_action(self) -> None:
        if self._move_key is not None or self._stop_error is not None:
            raise ServiceError(
                "confirm the previous motion and stop before sending another action"
            )
        if self._error is not None:
            raise ServiceError("a previous action failed; finish and report it")

    def _wait(
        self, record: dict[str, Any], deadline: float, *, interruptible: bool = True
    ) -> dict[str, Any]:
        while record["status"] == "queued":
            if interruptible:
                from_thread.check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"request {record['request_id']} did not finish in time"
                )
            time.sleep(min(_POLL_INTERVAL_S, remaining))
            if interruptible:
                from_thread.check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"request {record['request_id']} did not finish in time"
                )
            record = self._env.get_request(
                record["request_id"], operation=record["operation"], timeout_s=remaining
            )
        return record

    def _clear_motion(self) -> None:
        self._move_key = self._move_id = None

    def _lookup_rejected(
        self, error: RobotRequestRejected, *, operation: str, key: str
    ) -> dict[str, Any]:
        if error.request_id is None:
            raise error
        record = self._env.get_request(error.request_id, operation=operation)
        if record.get("client_request_id") != key:
            raise ServiceError(
                "rejected request ID belongs to a different submission"
            ) from error
        return record

    def _stop(self, *, explicit: bool) -> dict[str, Any]:
        deadline = time.monotonic() + _STOP_BUDGET_S
        self._ready()
        key = uuid4().hex
        try:
            record = self._env.submit_stop(key=key)
        except RobotRequestRejected as exc:
            self._stop_id = exc.request_id
            record = self._lookup_rejected(exc, operation="stop", key=key)
        self._stop_id = record["request_id"]
        self._persist()
        record = self._wait(record, deadline, interruptible=False)
        feedback = _feedback(record, "stop_result")
        if (
            record["status"] != "completed"
            or feedback.get("ok") is not True
            or feedback.get("stop_requested") is not True
        ):
            raise ServiceError(f"stop request {record['request_id']} did not complete")

        confirmed = (
            feedback.get("stop_confirmed") is True
            and feedback.get("active_task_may_continue") is False
        )
        if self._move_id is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "stop budget elapsed before checking the original move"
                )
            move = self._env.get_request(
                self._move_id, operation="move_relative", timeout_s=remaining
            )
            move = self._wait(move, deadline, interruptible=False)
            self._last_move_status = move["status"]
            confirmed = _motion_stopped(move)
            if _motion_resolved(move):
                self._clear_motion()
        if self._move_key is not None or (explicit and not confirmed):
            raise ServiceError(
                "stop was requested but physical stopping could not be confirmed"
            )
        self._stop_confirmed = confirmed
        self._stop_error = None
        self._persist()
        return {**_summary(record), "stop_confirmed": confirmed}

    def move_relative(
        self, dx_m: float, dy_m: float, arrival_turn_deg: float
    ) -> dict[str, Any]:
        """Turn toward a relative target, walk to it, then apply an extra turn.

        Args:
            dx_m: Forward (+) or rear (-) offset in the starting body frame, meters.
            dy_m: Left (+) or right (-) offset in that frame, meters.
            arrival_turn_deg: Extra turn from the actual arrival heading, degrees. Positive turns left.
        """
        self._allow_action()
        command = relative_command(dx_m, dy_m, arrival_turn_deg)
        self._ready(movement=True)
        from_thread.check_cancelled()
        key = uuid4().hex
        self._move_key = key
        self._last_move_id = self._last_move_status = None
        self._stop_id = self._stop_confirmed = None
        try:
            self._persist()
        except OSError:
            self._clear_motion()
            raise
        deadline = time.monotonic() + self._timeout_s
        try:
            try:
                record = self._env.submit_move(command, key=key)
            except RobotRequestRejected as exc:
                if exc.request_id is None:
                    self._clear_motion()
                    raise
                record = self._lookup_rejected(exc, operation="move_relative", key=key)
            self._move_id = record["request_id"]
            self._last_move_id = self._move_id
            self._last_move_status = record["status"]
            self._persist()
            record = self._wait(record, deadline)
            self._last_move_status = record["status"]
            if _motion_resolved(record):
                self._stop_confirmed = _motion_stopped(record)
                self._clear_motion()
            if not _completed_move(record):
                raise _failed(record, "relative move")
            self._completed += 1
            return _summary(record)
        finally:
            if self._move_key is not None:
                try:
                    self._stop(explicit=False)
                except (ServiceError, TimeoutError) as exc:
                    self._stop_error = str(exc)
                    self._stop_confirmed = False
            self._persist()

    def capture_image(
        self, rgbd: bool = False, after_motion: bool = False
    ) -> dict[str, Any]:
        """Wait for a capture and report metadata and files on the robot.

        Args:
            rgbd: Request aligned depth in addition to color.
            after_motion: Queue this capture behind previously submitted action commands.
        """
        self._allow_action()
        self._ready()
        from_thread.check_cancelled()
        deadline = time.monotonic() + self._timeout_s
        key = uuid4().hex
        try:
            record = self._env.submit_capture(
                rgbd=rgbd, after_motion=after_motion, key=key
            )
        except RobotRequestRejected as exc:
            record = self._lookup_rejected(exc, operation="capture_image", key=key)
        record = self._wait(record, deadline)
        feedback = _feedback(record, "capture_result")
        if (
            record["status"] != "completed"
            or feedback.get("status") != "captured"
            or type(feedback.get("exit_code")) is not int
            or feedback["exit_code"] != 0
            or not isinstance(feedback.get("files"), dict)
            or not isinstance(feedback["files"].get("color"), str)
            or not feedback["files"]["color"]
            or feedback.get("rgbd") is not rgbd
            or (
                rgbd
                and (
                    not isinstance(feedback["files"].get("depth"), str)
                    or not feedback["files"]["depth"]
                )
            )
        ):
            raise _failed(record, "capture")
        return _summary(record)

    def stop_robot(self) -> dict[str, Any]:
        """Request stop and queue clearing for the assigned robot, then check evidence."""
        self._stop_id = None
        self._stop_confirmed = None
        self._stop_error = "stop request is not yet confirmed"
        self._persist()
        try:
            return self._stop(explicit=True)
        except (ServiceError, TimeoutError) as exc:
            self._stop_error = str(exc)
            self._stop_confirmed = False
            raise

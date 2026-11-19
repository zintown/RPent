# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

"""HTTP client for the Unitree communication service's asynchronous /call API."""

from __future__ import annotations

import math
from typing import Any

import httpx

from robots.unitree.http_client import ServiceError, finite_number, validate_endpoint
from robots.unitree.protocol import MAX_DISTANCE_M, validate_robot_index

_REQUEST_STATUSES = ("queued", "completed", "dry_run", "failed", "rejected", "unknown")
_OPERATIONS = ("move_relative", "capture_image", "stop")


class RobotRequestRejected(ServiceError):
    """A rejected RPC may still have a request record to reconcile."""

    def __init__(self, message: str, request_id: str | None) -> None:
        super().__init__(message)
        self.request_id = request_id


def relative_command(
    dx_m: float, dy_m: float, arrival_turn_deg: float
) -> dict[str, Any]:
    """Validate finite body-frame offsets and preserve the full relative turn."""
    payload = {
        "dx_m": finite_number(dx_m, "dx_m"),
        "dy_m": finite_number(dy_m, "dy_m"),
        "arrival_turn_deg": finite_number(arrival_turn_deg, "arrival_turn_deg"),
    }
    if math.hypot(payload["dx_m"], payload["dy_m"]) > MAX_DISTANCE_M:
        raise ValueError("relative displacement must not exceed 5 m in total")
    return {"type": "move_relative", "payload": payload}


class RealRobotClient:
    """Borrow a communication service and route actions to one configured index."""

    def __init__(
        self, endpoint: str, *, robot_index: int, client: httpx.Client | None = None
    ) -> None:
        validate_endpoint(endpoint)
        validate_robot_index(robot_index)
        self.endpoint = endpoint.rstrip("/")
        self.robot_index = robot_index
        self._http = client if client is not None else httpx.Client(trust_env=False)
        self._owns_client = client is None

    def _call(
        self,
        method: str,
        *,
        args: list[dict[str, Any]] | None = None,
        kwargs: dict[str, Any] | None = None,
        key: str | None = None,
        timeout_s: float = 4.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "method": method,
            "args": [] if args is None else args,
            "kwargs": {} if kwargs is None else kwargs,
            "session_id": None,
        }
        if key is not None:
            payload.update(request_id=key, idempotency_key=key)
        try:
            response = self._http.post(
                self.endpoint + "/call", json=payload, timeout=min(timeout_s, 4.0)
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ServiceError(f"{method}: {exc}") from exc
        if not isinstance(data, dict) or type(data.get("ok")) is not bool:
            raise ServiceError(f"{method}: response must contain boolean ok")
        result = data.get("result")
        if not data["ok"]:
            request_id = data.get("request_id")
            if request_id is None and isinstance(result, dict):
                request_id = result.get("request_id")
            if request_id is not None and (
                not isinstance(request_id, str) or not request_id.strip()
            ):
                raise ServiceError(f"{method}: invalid request ID in rejection")
            raise RobotRequestRejected(f"{method}: {data.get('error')}", request_id)
        if not isinstance(result, dict):
            raise ServiceError(f"{method}: response must contain an object result")
        return result

    def list_robots(self) -> dict[str, Any]:
        """Read the communication service's current online robot list."""
        result = self._call("env.list_robots")
        robots = result.get("robots")
        if (
            not isinstance(robots, list)
            or type(result.get("total")) is not int
            or result["total"] != len(robots)
        ):
            raise ServiceError("invalid online robot list")
        previous = 0
        for entry in robots:
            if (
                not isinstance(entry, dict)
                or type(entry.get("robot_index")) is not int
                or entry["robot_index"] <= previous
                or entry.get("status") != "online"
            ):
                raise ServiceError("online robot indices must be unique and increasing")
            previous = entry["robot_index"]
        return result

    def _check_robot(self, result: dict[str, Any]) -> None:
        if (
            type(result.get("robot_index")) is not int
            or result["robot_index"] != self.robot_index
        ):
            raise ServiceError("response belongs to a different robot index")

    def health(self) -> dict[str, Any]:
        """Read cached connectivity, heartbeat and camera status."""
        result = self._call("healthz", kwargs={"robot_index": self.robot_index})
        self._check_robot(result)
        if result.get("status") not in ("online", "offline"):
            raise ServiceError("unexpected robot connection status")
        return result

    def _record(
        self,
        record: dict[str, Any],
        *,
        operation: str | None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self._check_robot(record)
        uid = record.get("request_id")
        if (
            not isinstance(uid, str)
            or not uid.strip()
            or (request_id is not None and uid != request_id)
        ):
            raise ServiceError("response has a missing or mismatched request ID")
        if record.get("operation") not in _OPERATIONS or (
            operation is not None and record["operation"] != operation
        ):
            raise ServiceError("response has a mismatched robot operation")
        if record.get("status") not in _REQUEST_STATUSES:
            raise ServiceError("unexpected robot request status")
        available = record.get("result_available")
        known = record.get("outcome_known")
        if type(available) is not bool or type(known) is not bool:
            raise ServiceError(
                "request record must specify result availability and outcome"
            )
        if available:
            feedback = record.get("result")
            if not isinstance(feedback, dict) or feedback.get("request_id") != uid:
                raise ServiceError("request feedback belongs to a different request")
            self._check_robot(feedback)
        return record

    def _submit(
        self,
        method: str,
        operation: str,
        args: list[dict[str, Any]],
        key: str,
    ) -> dict[str, Any]:
        record = self._record(
            self._call(
                method, args=args, kwargs={"robot_index": self.robot_index}, key=key
            ),
            operation=operation,
        )
        if record.get("client_request_id") != key:
            raise ServiceError(
                f"{method}: client request ID does not match the submission"
            )
        return record

    def submit_move(self, command: dict[str, Any], *, key: str) -> dict[str, Any]:
        """Submit one validated move_relative command without retrying it."""
        return self._submit("env.execute_command", "move_relative", [command], key)

    def submit_capture(
        self, *, rgbd: bool, after_motion: bool, key: str
    ) -> dict[str, Any]:
        """Submit a capture and retain the server-assigned request ID."""
        return self._submit(
            "env.capture_image",
            "capture_image",
            [{"rgbd": rgbd, "after_motion": after_motion}],
            key,
        )

    def submit_stop(self, *, key: str) -> dict[str, Any]:
        """Request queue clearing and stopping for the configured robot."""
        return self._submit("env.stop", "stop", [], key)

    def get_request(
        self,
        request_id: str,
        *,
        operation: str | None = None,
        timeout_s: float = 4.0,
    ) -> dict[str, Any]:
        """Read a server request record for the configured robot."""
        if not request_id.strip():
            raise ValueError("request_id must be the server request ID")
        return self._record(
            self._call(
                "env.get_request",
                kwargs={"request_id": request_id},
                timeout_s=timeout_s,
            ),
            request_id=request_id,
            operation=operation,
        )

    def close(self) -> None:
        """Release the local HTTP client."""
        if self._owns_client:
            self._http.close()

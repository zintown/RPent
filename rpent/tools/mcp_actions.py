"""Track asynchronous MCP actions using their configured status contract."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any

from mcp import types
from pydantic import BaseModel, ConfigDict

from rpent.evaluation.result import write_json_atomic
from rpent.tools.mcp_client import (
    McpActionStatusConfig,
    McpClient,
    McpServiceError,
    check_matches,
    mcp_result_object,
)


def _field(data: dict[str, Any], path: str) -> Any:
    if path in data:
        return data[path]
    value: Any = data
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


class McpActionReceipt(BaseModel):
    """Persist an attempted action and any evidence observed by the worker."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action_id: str | int | None = None
    status: dict[str, Any] | None = None
    rejection: dict[str, Any] | None = None

    @property
    def resolved(self) -> bool:
        return self.status is not None or self.rejection is not None

    def matches(self, config: McpActionStatusConfig, status: dict[str, Any]) -> bool:
        """Confirm that a status is for the action accepted by this task."""
        action_id = _field(status, config.status_id_field or config.id_field)
        return (
            self.action_id is not None
            and type(action_id) is type(self.action_id)
            and action_id == self.action_id
        )

    def is_terminal(
        self, config: McpActionStatusConfig, status: dict[str, Any]
    ) -> bool:
        """Check the configured terminal condition on the owned action."""
        return self.matches(config, status) and all(
            check_matches(status, key, value) for key, value in config.terminal.items()
        )


def read_action_status(
    client: McpClient,
    config: McpActionStatusConfig,
    receipt: McpActionReceipt,
    *,
    timeout_s: float,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Sample an owned action or wait for it within an independent deadline."""
    arguments = (
        {config.status_id_argument: receipt.action_id}
        if config.status_id_argument is not None
        else {}
    )
    deadline = time.monotonic() + timeout_s
    while True:
        if check_cancelled is not None:
            check_cancelled()
        call_timeout = (
            min(8.0, max(0.01, deadline - time.monotonic())) if timeout_s else 8.0
        )
        status = client.read_check(
            config.status_tool,
            arguments,
            timeout_s=call_timeout,
            check_cancelled=check_cancelled,
        )
        if not receipt.matches(config, status) or receipt.is_terminal(config, status):
            return status
        next_poll = min(deadline, time.monotonic() + config.poll_interval_sec)
        while remaining := next_poll - time.monotonic():
            if remaining <= 0:
                break
            if check_cancelled is not None:
                check_cancelled()
            time.sleep(
                min(remaining, 0.1) if check_cancelled is not None else remaining
            )
        if time.monotonic() >= deadline:
            return status


class McpActionTracker:
    """Record submission before dispatch and confirm outcomes by action ID."""

    def __init__(self, config: McpActionStatusConfig, path: Path | None) -> None:
        self.config = config
        self._path = path
        self._receipt: McpActionReceipt | None = None
        self._lock = Lock()

    def begin(self) -> None:
        """Reserve a submission and leave evidence even if its reply is lost."""
        with self._lock:
            if self._receipt is not None and not self._receipt.resolved:
                raise McpServiceError(
                    f"Verify the previous action with {self.config.status_tool} "
                    "before starting another action"
                )
            self._store(McpActionReceipt())

    def observe(self, tool: str, result: types.CallToolResult) -> None:
        """Update the receipt from the start reply or an owned terminal status."""
        data = mcp_result_object(result)
        if data is None:
            return
        with self._lock:
            receipt = self._receipt
            if receipt is None or receipt.resolved:
                return
            config = self.config
            if tool == config.start_tool:
                action_id = _field(data, config.id_field)
                if (
                    isinstance(action_id, (str, int))
                    and not isinstance(action_id, bool)
                    and action_id != ""
                ):
                    self._store(McpActionReceipt(action_id=action_id))
                elif config.rejected and all(
                    check_matches(data, key, value)
                    for key, value in config.rejected.items()
                ):
                    self._store(McpActionReceipt(rejection=data))
            elif (
                tool == config.status_tool
                and not result.isError
                and receipt.is_terminal(config, data)
            ):
                self._store(receipt.model_copy(update={"status": data}))

    def wait(
        self, client: McpClient, check_cancelled: Callable[[], None]
    ) -> dict[str, Any] | None:
        """Return the status of the accepted action without involving the model."""
        with self._lock:
            receipt = self._receipt
        if receipt is None or receipt.action_id is None:
            return None
        status = read_action_status(
            client,
            self.config,
            receipt,
            timeout_s=self.config.completion_timeout_sec,
            check_cancelled=check_cancelled,
        )
        terminal = receipt.is_terminal(self.config, status)
        if terminal:
            with self._lock:
                if self._receipt is receipt:
                    self._store(receipt.model_copy(update={"status": status}))
        return {
            "action_id": receipt.action_id,
            "state": "finished" if terminal else "unconfirmed",
            "ok": terminal
            and all(
                check_matches(status, key, value)
                for key, value in self.config.success.items()
            ),
            "status": status,
        }

    def _store(self, receipt: McpActionReceipt) -> None:
        if self._path is not None:
            self._path.parent.mkdir(exist_ok=True)
            write_json_atomic(self._path, receipt.model_dump(exclude_none=True))
        self._receipt = receipt

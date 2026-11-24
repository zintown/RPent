"""Own downstream MCP sessions and share their tools within a task or probe."""

from __future__ import annotations

import base64
import os
from collections.abc import Callable, Iterable, Mapping
from contextlib import ExitStack, closing
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Annotated, Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from mcp import types
from pydantic import BaseModel, ConfigDict, Field

from rpent.evaluation.result import write_json_atomic
from rpent.tools.base import Tool, ToolResult, tool
from rpent.tools.mcp_actions import McpActionTracker
from rpent.tools.mcp_client import (
    McpActionStatusConfig,
    McpClient,
    McpServerConfig,
    McpServiceError,
    check_matches,
    mcp_result_object,
    mcp_tool_name,
)
from rpent.utils.logging import get_logger

logger = get_logger("mcp")


class McpToolInputError(McpServiceError):
    """A request was rejected locally before invoking the downstream tool."""


class _ResultReference(BaseModel):
    """Select a value from a successful MCP result in the current task."""

    model_config = ConfigDict(extra="forbid", strict=True)

    result_id: Annotated[str, Field(min_length=1)]
    path: list[str | int] = Field(default_factory=list)


@dataclass(frozen=True)
class McpService:
    """An open session with its allowed and model-visible tool catalogs."""

    _client: McpClient
    tools: Mapping[str, types.Tool]
    model_tools: Mapping[str, types.Tool]
    _action_marker: Path | None = None
    _action: McpActionTracker | None = None

    @property
    def name(self) -> str:
        return self._client.name

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        check_cancelled: Callable[[], None] | None = None,
        timeout_s: float | None = None,
    ) -> types.CallToolResult:
        """Validate the request and return the full MCP result to the caller."""
        definition = self.tools.get(name)
        if definition is None:
            raise McpToolInputError(f"{self.name}.{name} is not enabled")
        validator = Draft202012Validator(definition.inputSchema)
        if problem := next(validator.iter_errors(arguments), None):
            raise McpToolInputError(
                f"{self.name}.{name} input does not match its schema: {problem.message}"
            )
        if check_cancelled is not None:
            check_cancelled()
        if self._action is not None and name == self._action.config.start_tool:
            status_tool = self._action.config.status_tool
            if not self._client.is_readonly_tool(status_tool):
                raise McpToolInputError(f"{self.name}.{status_tool} must be read-only")
            if self._action.config.status_id_argument is None:
                self._client.require_readonly_tool(status_tool)
            self._action.begin()
        if self._action_marker is not None and not self._client.is_readonly_tool(name):
            self._action_marker.parent.mkdir(exist_ok=True)
            self._action_marker.touch()
        result = self._client.call_tool(
            name, arguments, check_cancelled=check_cancelled, timeout_s=timeout_s
        )
        if self._action is not None:
            self._action.observe(name, result)
        return result

    def read_check(self, name: str) -> dict[str, Any]:
        """Run a configured, no-argument read-only capability check."""
        return self._client.read_check(name)

    def wait_for_action(
        self, name: str, check_cancelled: Callable[[], None]
    ) -> dict[str, Any] | None:
        """Wait for configured model-facing submissions through the same session."""
        if self._action is None or name != self._action.config.start_tool:
            return None
        if self._action.config.wait_for_terminal:
            return self._action.wait(self._client, check_cancelled)
        return None


@dataclass
class _ServerSlot:
    config: McpServerConfig
    lock: Lock = field(default_factory=Lock)
    connections: ExitStack = field(default_factory=ExitStack)
    service: McpService | None = None
    error: str | None = None


class McpServiceRegistry:
    """Connect once per configured alias within the owning task or readiness probe."""

    def __init__(
        self,
        servers: Mapping[str, McpServerConfig] | None = None,
        *,
        probe_output: Path | None = None,
    ) -> None:
        self._slots: dict[str, _ServerSlot] = {}
        self._probe_output = probe_output
        self._run_id = (
            os.environ.get("RPENT_MCP_RUN_ID") if probe_output is None else None
        )
        self._task_output = os.environ.get("RPENT_TASK_OUTPUT_DIR")
        self._results: dict[str, dict[str, Any]] = {}
        self._model_calls: dict[str, Callable[..., ToolResult]] = {}
        self._preflight: dict[str, list[dict[str, Any] | None]] = {}
        self._closed = False
        self.register(servers or {})

    def register(self, servers: Mapping[str, McpServerConfig]) -> None:
        """Add service configuration before the registry is used."""
        if self._closed:
            raise RuntimeError("MCP registry is closed")
        duplicates = self._slots.keys() & servers.keys()
        if duplicates:
            raise ValueError(
                f"MCP services are already configured: {sorted(duplicates)}"
            )
        self._slots.update(
            {name: _ServerSlot(config) for name, config in servers.items()}
        )
        for config in servers.values():
            if config.action_status is not None:
                for reference in config.action_status.preflight:
                    self._preflight.setdefault(reference, [])

    def connect(self, name: str) -> McpService:
        """Open or reuse a service session and validate its enabled tools."""
        slot = self._slots.get(name)
        if slot is None:
            raise McpServiceError(f"Unknown MCP service {name!r}")
        with slot.lock:
            if self._closed:
                raise McpServiceError("MCP registry is closed")
            config = slot.config
            if not config.enabled or config.enabled_tools == []:
                raise McpServiceError(f"MCP service {name!r} has no enabled tools")
            if slot.service is not None:
                return slot.service
            if slot.error is not None:
                raise McpServiceError(slot.error)
            try:
                slot.service = self._open(name, config, slot.connections)
            except McpServiceError as exc:
                slot.error = str(exc)
                raise
            return slot.service

    def _open(
        self, name: str, config: McpServerConfig, connections: ExitStack
    ) -> McpService:
        env_overrides = None
        if self._probe_output is not None:
            config = config.model_copy(
                update={
                    "startup_timeout_sec": min(config.startup_timeout_sec, 5),
                    "tool_timeout_sec": min(config.tool_timeout_sec, 8),
                }
            )
            env_overrides = {"RPENT_TASK_OUTPUT_DIR": str(self._probe_output)}
        run_id = self._run_id if config.run_report_tool else None
        marker = (
            Path(self._task_output) / ".mcp-actions" / name
            if self._run_id and self._task_output
            else None
        )
        action = (
            McpActionTracker(config.action_status, marker)
            if config.action_status is not None and self._probe_output is None
            else None
        )
        with ExitStack() as pending:
            client = pending.enter_context(
                closing(
                    McpClient(name, config, env_overrides=env_overrides, run_id=run_id)
                )
            )
            tools = client.select_tools(config)
            for raw_name, definition in tools.items():
                try:
                    Draft202012Validator.check_schema(definition.inputSchema)
                except SchemaError as exc:
                    raise McpServiceError(
                        f"{name}.{raw_name} has an invalid input schema"
                    ) from exc
            model_names = (
                tools.keys() if config.model_tools is None else config.model_tools
            )
            missing = set(model_names) - tools.keys()
            if missing:
                raise McpServiceError(
                    f"MCP service {name!r} does not enable model tools {sorted(missing)}"
                )
            service = McpService(
                client,
                tools,
                {raw_name: tools[raw_name] for raw_name in model_names},
                marker if run_id else None,
                action,
            )
            connections.enter_context(pending.pop_all())
        return service

    @property
    def errors(self) -> dict[str, str]:
        return {
            name: slot.error
            for name, slot in self._slots.items()
            if slot.error is not None
        }

    def model_tools(
        self, names: Iterable[str], check_cancelled: Callable[[], None]
    ) -> list[Tool]:
        """Register healthy model-facing tools independently of other services."""
        declarations = []
        add_reference_tool = not self._model_calls
        for name in names:
            config = self._slots[name].config
            if (
                not config.enabled
                or config.enabled_tools == []
                or config.model_tools == []
            ):
                continue
            try:
                service = self.connect(name)
            except McpServiceError as exc:
                logger.warning("Skipping MCP tools from %s: %s", name, exc)
                continue
            for raw_name, definition in service.model_tools.items():
                public_name = mcp_tool_name(name, raw_name)
                handler = partial(self._invoke, service, raw_name, check_cancelled)
                self._model_calls[public_name] = handler
                description = definition.description or raw_name
                action = config.action_status
                if action is not None and raw_name == action.start_tool:
                    if action.wait_for_terminal:
                        description += (
                            "\nRPent waits for the accepted action and returns its "
                            "outcome in the action field. Use that evidence before "
                            "requesting another action."
                        )
                    if action.preflight:
                        description += (
                            "\nRequired preflight tools: "
                            + ", ".join(
                                mcp_tool_name(*reference.split(".", 1))
                                for reference in action.preflight
                            )
                            + ". A failed preflight blocks this action for the task."
                        )
                declarations.append(
                    Tool(
                        name=public_name,
                        description=description,
                        args_schema=None,
                        handler=handler,
                        _input_schema=definition.inputSchema,
                        capture_state=False,
                    )
                )
        if declarations and add_reference_tool:
            declarations.append(replace(self._call_with_refs, capture_state=False))
        return declarations

    @tool(name="mcp_call_with_refs")
    def _call_with_refs(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        references: Annotated[dict[str, _ResultReference], Field(min_length=1)],
    ) -> ToolResult:
        """Call a visible MCP tool with values from earlier structured results.

        Args:
            tool_name: Name of a discovered tool, such as mcp__server__tool.
            arguments: Literal argument values. Use an empty object if all are references.
            references: Map each remaining argument to a result_id and optional path.
                Omit path to pass the whole structured_content. Use field names or
                array indices in path to select a nested value.
        """
        handler = self._model_calls.get(tool_name)
        if handler is None:
            return ToolResult(data={"error": f"Unknown model MCP tool: {tool_name}"})
        if overlap := arguments.keys() & references.keys():
            return ToolResult(
                data={
                    "error": f"Arguments with both values and references: {sorted(overlap)}"
                }
            )
        try:
            bound = {
                key: self._resolve(reference) for key, reference in references.items()
            }
        except McpToolInputError as exc:
            return ToolResult(data={"error": str(exc)})
        return handler(**arguments, **bound)

    def _resolve(self, reference: _ResultReference) -> Any:
        value: Any = self._results.get(reference.result_id)
        if value is None:
            raise McpToolInputError(f"Unknown MCP result ID: {reference.result_id}")
        for part in reference.path:
            if isinstance(value, dict) and isinstance(part, str) and part in value:
                value = value[part]
            elif (
                isinstance(value, list) and type(part) is int and 0 <= part < len(value)
            ):
                value = value[part]
            else:
                raise McpToolInputError(
                    f"Result {reference.result_id} has no value at {reference.path!r}"
                )
        return value

    def _check_preflight(self, action: McpActionStatusConfig) -> None:
        for reference, expected in action.preflight.items():
            results = self._preflight[reference]
            if not results:
                raise McpToolInputError(f"Run the required preflight tool {reference}")
            if any(
                data is None
                or not all(
                    check_matches(data, key, value) for key, value in expected.items()
                )
                for data in results
            ):
                error = f"Required preflight {reference} failed for this task"
                if self._run_id and self._task_output:
                    write_json_atomic(
                        Path(self._task_output) / ".mcp-preflight.json",
                        {"source": reference, "error": error},
                    )
                raise McpToolInputError(error)

    def close(self) -> None:
        """Release every session after the owner has drained its calls."""
        self._closed = True
        self._model_calls.clear()
        self._results.clear()
        self._preflight.clear()
        with ExitStack() as connections:
            for slot in self._slots.values():
                with slot.lock:
                    connections.enter_context(slot.connections.pop_all())

    def _invoke(
        self,
        service: McpService,
        name: str,
        check_cancelled: Callable[[], None],
        /,
        **arguments: Any,
    ) -> ToolResult:
        reference = f"{service.name}.{name}"
        preflight = self._preflight.get(reference)
        action = self._slots[service.name].config.action_status
        try:
            if action is not None and name == action.start_tool:
                self._check_preflight(action)
            result = service.call_tool(name, arguments, check_cancelled=check_cancelled)
        except McpServiceError as exc:
            if preflight is not None and not isinstance(exc, McpToolInputError):
                preflight.append(None)
            return ToolResult(data={"error": str(exc)})
        data: dict[str, Any] = {}
        images = []
        content = []
        messages = []
        structured = mcp_result_object(result)
        if preflight is not None:
            preflight.append(None if result.isError else structured or {})
        if structured is not None and not result.isError:
            result_id = uuid4().hex
            self._results[result_id] = structured
            data["result_id"] = result_id
        if structured is not None:
            data["structured_content"] = structured
        for block in result.content:
            if isinstance(block, types.TextContent):
                content.append({"type": "text", "text": block.text})
                messages.append(block.text)
            elif (
                isinstance(block, types.ImageContent) and block.mimeType == "image/png"
            ):
                images.append(base64.b64decode(block.data, validate=True))
            else:
                content.append(
                    {"type": block.type, "notice": "MCP content type unavailable"}
                )
        if content:
            data["content"] = content
        if result.isError:
            data["error"] = (
                "\n".join(messages) or f"MCP {service.name}/{name} returned an error"
            )
        else:
            try:
                outcome = service.wait_for_action(name, check_cancelled)
            except McpServiceError as exc:
                data["error"] = str(exc)
            else:
                if outcome is not None:
                    data["action"] = outcome
                    if not outcome["ok"]:
                        data["error"] = (
                            f"{reference} action {outcome['action_id']} did not confirm success"
                        )
        return ToolResult(data=data, images=images)

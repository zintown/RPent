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

"""Connect configured MCP servers to RPent tools."""

from __future__ import annotations

import base64
import json
import os
import re
from collections import ChainMap
from collections.abc import AsyncIterator, Callable, Mapping
from concurrent.futures import wait
from contextlib import ExitStack, asynccontextmanager
from functools import partial
from hashlib import sha256
from io import BytesIO
from uuid import uuid4
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from anyio import fail_after
from anyio.from_thread import start_blocking_portal
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from jsonschema import Draft202012Validator
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.message import SessionMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rpent.tools.base import Tool, ToolResult

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


class McpServerConfig(BaseModel):
    """Connection and tool visibility settings for one MCP server."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    transport: Literal["streamable-http", "stdio"] = "streamable-http"
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env_from: list[str] = Field(default_factory=list)
    enabled: bool = True
    enabled_tools: list[str] | None = None
    startup_timeout_sec: float = Field(default=5, gt=0, allow_inf_nan=False)
    tool_timeout_sec: float = Field(default=10, gt=0, allow_inf_nan=False)
    bearer_token_env_var: str | None = None
    run_report_tool: str | None = Field(default=None, min_length=1)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.fragment
        ):
            raise ValueError(
                "url must be an HTTP(S) MCP endpoint without credentials or a fragment"
            )
        _ = parsed.port
        return value

    @field_validator("env_from")
    @classmethod
    def validate_env_names(cls, names: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names):
            raise ValueError("env_from must contain environment variable names")
        return names

    @model_validator(mode="after")
    def validate_transport(self) -> McpServerConfig:
        if self.transport == "stdio":
            if not self.command or not self.command.strip():
                raise ValueError("stdio requires a command")
            if self.url is not None or self.bearer_token_env_var is not None:
                raise ValueError("url and bearer_token_env_var apply to HTTP MCP")
            if self.run_report_tool is not None:
                raise ValueError(
                    "run_report_tool requires a persistent HTTP MCP service"
                )
        else:
            if self.url is None:
                raise ValueError("streamable-http requires a url")
            if (
                self.command is not None
                or self.args
                or self.cwd is not None
                or self.env_from
            ):
                raise ValueError("command, args, cwd and env_from apply to stdio MCP")
        return self


def validate_mcp_server_name(name: str) -> str:
    """Check the server name used in configuration and tool namespaces."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(f"invalid MCP server name {name!r}")
    return name


def read_mcp_config(path: Path) -> dict[str, Any]:
    """Read the TOML shared by task deployments and MCP tool discovery."""
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_mcp_servers(path: Path) -> dict[str, McpServerConfig]:
    """Read server entries from a TOML configuration file."""
    path = path.expanduser().resolve()
    return parse_mcp_servers(read_mcp_config(path), path)


def parse_mcp_servers(config: dict[str, Any], path: Path) -> dict[str, McpServerConfig]:
    """Validate an MCP configuration and resolve explicit working directories."""
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        raise ValueError(f"{path} must contain an [mcp_servers] table")
    result = {}
    for name, entry in servers.items():
        validate_mcp_server_name(name)
        server = McpServerConfig.model_validate(entry)
        if server.cwd is not None:
            cwd = (path.parent / Path(server.cwd).expanduser()).resolve()
            server = server.model_copy(update={"cwd": str(cwd)})
        result[name] = server
    return result


class McpServiceError(RuntimeError):
    """An MCP service could not be discovered or called."""


def _error_message(error: Exception) -> str:
    while getattr(error, "exceptions", ()):
        error = error.exceptions[0]
    return str(error) or type(error).__name__


@asynccontextmanager
async def _transport(
    config: McpServerConfig, headers: dict[str, str], environment: Mapping[str, str]
) -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    if config.transport == "stdio":
        stdio_env = {}
        for name in config.env_from:
            if name not in environment:
                raise ValueError(f"stdio requires environment variable {name}")
            stdio_env[name] = environment[name]
        assert config.command is not None
        params = StdioServerParameters(
            command=config.command, args=config.args, cwd=config.cwd, env=stdio_env
        )
        async with stdio_client(params) as streams:
            yield streams
        return

    assert config.url is not None
    timeout = httpx.Timeout(config.startup_timeout_sec, read=config.tool_timeout_sec)
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, trust_env=False
    ) as http:
        async with streamable_http_client(config.url, http_client=http) as (
            read,
            write,
            _,
        ):
            yield read, write


@asynccontextmanager
async def _connect(
    config: McpServerConfig, headers: dict[str, str], environment: Mapping[str, str]
) -> AsyncIterator[tuple[ClientSession, dict[str, types.Tool]]]:
    async with _transport(config, headers, environment) as (read, write):
        async with ClientSession(read, write) as session:
            tools: dict[str, types.Tool] = {}
            with fail_after(config.startup_timeout_sec):
                info = await session.initialize()
                if info.capabilities.tools is not None:
                    cursor = None
                    seen_cursors: set[str] = set()
                    while True:
                        page = await session.list_tools(cursor=cursor)
                        for definition in page.tools:
                            if definition.name in tools:
                                raise ValueError(
                                    f"duplicate MCP tool {definition.name!r}"
                                )
                            tools[definition.name] = definition
                        cursor = page.nextCursor
                        if cursor is None:
                            break
                        if cursor in seen_cursors:
                            raise ValueError("MCP server repeated a tool-list cursor")
                        seen_cursors.add(cursor)
            yield session, tools


class McpClient:
    """Own one MCP connection for synchronous toolkit or navigation callers."""

    def __init__(
        self,
        name: str,
        config: McpServerConfig,
        *,
        env_overrides: Mapping[str, str] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.name = name
        self._timeout = config.tool_timeout_sec
        self._run_id = run_id
        environment = ChainMap(env_overrides or {}, os.environ)
        headers = {}
        if config.bearer_token_env_var is not None:
            token = environment.get(config.bearer_token_env_var)
            if not token:
                raise McpServiceError(
                    f"MCP server {name!r} requires {config.bearer_token_env_var}"
                )
            headers["Authorization"] = f"Bearer {token}"
        try:
            with ExitStack() as connections:
                self._portal = connections.enter_context(start_blocking_portal())
                context = self._portal.wrap_async_context_manager(
                    _connect(config, headers, environment)
                )
                self._session, self.tools = connections.enter_context(context)
                self._connections = connections.pop_all()
        except Exception as exc:
            raise McpServiceError(
                f"MCP server {name!r} startup failed: {_error_message(exc)}"
            ) from exc

    async def _call(
        self, name: str, arguments: dict[str, Any], timeout_s: float
    ) -> types.CallToolResult:
        try:
            with fail_after(timeout_s):
                if self._run_id is not None:
                    return await self._session.call_tool(
                        name, arguments, meta={"rpent.dev/run-id": self._run_id}
                    )
                return await self._session.call_tool(name, arguments)
        except Exception as exc:
            raise McpServiceError(
                f"MCP {self.name}/{name} failed: {_error_message(exc)}"
            ) from exc

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        check_cancelled: Callable[[], None] | None = None,
        timeout_s: float | None = None,
    ) -> types.CallToolResult:
        """Call a discovered tool once within the configured deadline."""
        if name not in self.tools:
            raise McpServiceError(f"MCP server {self.name!r} does not export {name!r}")
        timeout = self._timeout if timeout_s is None else min(timeout_s, self._timeout)
        call = self._portal.start_task_soon(self._call, name, arguments, timeout)
        try:
            while check_cancelled is not None and not call.done():
                check_cancelled()
                wait((call,), timeout=0.05)
            return call.result()
        finally:
            if not call.done():
                call.cancel()

    def close(self) -> None:
        """Release the MCP session and its worker."""
        self._connections.close()

    def select_tools(self, config: McpServerConfig) -> dict[str, types.Tool]:
        """Resolve the planner's allowlist and reserve the reporting tool."""
        if config.run_report_tool is not None:
            self.require_readonly_tool(config.run_report_tool)
        names = self.tools if config.enabled_tools is None else config.enabled_tools
        selected = {}
        for name in names:
            if name == config.run_report_tool:
                continue
            if name not in self.tools:
                raise McpServiceError(
                    f"MCP server {self.name!r} does not export {name!r}"
                )
            selected[name] = self.tools[name]
        return selected

    def require_readonly_tool(self, name: str) -> None:
        """Validate an exported read-only tool that can be called without arguments."""
        definition = self.tools.get(name)
        if not (
            definition
            and definition.annotations
            and definition.annotations.readOnlyHint is True
            and Draft202012Validator(definition.inputSchema).is_valid({})
        ):
            raise McpServiceError(
                f"{self.name}.{name} must be an exported read-only tool callable without arguments"
            )

    def read_check(self, name: str) -> dict[str, Any]:
        """Call a read-only check with a short deadline and decode its object result."""
        self.require_readonly_tool(name)
        result = self.call_tool(name, {}, timeout_s=8)
        message = next(
            (
                block.text
                for block in result.content
                if isinstance(block, types.TextContent)
            ),
            "",
        )
        if result.isError:
            raise McpServiceError(message or f"{self.name}.{name} returned an error")
        if result.structuredContent is not None:
            return result.structuredContent
        if len(result.content) == 1 and message:
            try:
                data = json.loads(message)
            except ValueError as exc:
                raise McpServiceError(
                    f"{self.name}.{name} returned invalid JSON"
                ) from exc
            if isinstance(data, dict):
                return data
        raise McpServiceError(f"{self.name}.{name} must return a JSON object")


def check_matches(
    data: dict[str, Any], key: str, expected: bool | int | float | str
) -> bool:
    """Match a check field using its dotted path, scalar value and type."""
    value: Any = data
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return type(value) is type(expected) and value == expected


def mcp_tool_name(server: str, remote_name: str) -> str:
    """Return the planner and transcript name for a tool on a configured server."""
    name = f"mcp__{server}__{remote_name}"
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    if len(name) > 64 or normalized != name:
        return normalized[:54] + "__" + sha256(name.encode()).hexdigest()[:8]
    return name


def _invoke(
    client: McpClient,
    name: str,
    check_cancelled: Callable[[], None],
    action_marker: Path | None,
    /,
    **arguments: Any,
) -> ToolResult:
    try:
        check_cancelled()
        if action_marker is not None:
            action_marker.parent.mkdir(exist_ok=True)
            action_marker.touch()
        result = client.call_tool(name, arguments, check_cancelled=check_cancelled)
    except McpServiceError as exc:
        return ToolResult(data={"error": str(exc)})
    data: dict[str, Any] = {}
    images = []
    content = []
    messages = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            content.append({"type": "text", "text": block.text})
            messages.append(block.text)
        elif isinstance(block, types.ImageContent) and block.mimeType in {"image/png", "image/jpeg"}:
            raw = base64.b64decode(block.data, validate=True)
            if block.mimeType == "image/jpeg":
                from PIL import Image

                # ToolResult consumers expect PNG bytes, including the API planner.
                with Image.open(BytesIO(raw)) as photo, BytesIO() as output:
                    photo.convert("RGB").save(output, format="PNG")
                    images.append(output.getvalue())
            else:
                images.append(raw)
            photo_dir = os.environ.get("GO2_WEB_PHOTO_DIR")
            if photo_dir and name == "observe":
                suffix = ".jpg" if block.mimeType == "image/jpeg" else ".png"
                path = Path(photo_dir) / (uuid4().hex + suffix)
                pending = path.with_suffix(".tmp")
                pending.write_bytes(raw)
                pending.replace(path)
                content.append({"type": "text", "text": "照片已保存到当前网页任务。"})
        else:
            content.append(
                {"type": block.type, "notice": "MCP content type unavailable"}
            )
    if content:
        data["content"] = content
    if result.structuredContent is not None:
        data["structured_content"] = result.structuredContent
    if result.isError:
        data["error"] = (
            "\n".join(messages) or f"MCP {client.name}/{name} returned an error"
        )
    return ToolResult(data=data, images=images)


def discover_mcp_tools(
    servers: dict[str, McpServerConfig],
    connections: ExitStack,
    check_cancelled: Callable[[], None],
) -> list[Tool]:
    """Connect enabled servers and build declarations from their JSON Schemas."""
    declarations = []
    run_id = os.environ.get("RPENT_MCP_RUN_ID")
    output = os.environ.get("RPENT_TASK_OUTPUT_DIR")
    for name, config in servers.items():
        if not config.enabled or config.enabled_tools == []:
            continue
        client = McpClient(
            name, config, run_id=run_id if config.run_report_tool else None
        )
        connections.callback(client.close)
        for raw_name, definition in client.select_tools(config).items():
            Draft202012Validator.check_schema(definition.inputSchema)
            readonly = (
                definition.annotations and definition.annotations.readOnlyHint is True
            )
            marker = (
                Path(output) / ".mcp-actions" / name
                if run_id and output and config.run_report_tool and not readonly
                else None
            )
            declarations.append(
                Tool(
                    name=mcp_tool_name(name, raw_name),
                    description=definition.description or raw_name,
                    args_schema=None,
                    handler=partial(_invoke, client, raw_name, check_cancelled, marker),
                    _input_schema=definition.inputSchema,
                    capture_state=False,
                )
            )
    return declarations

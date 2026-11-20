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

"""Connect configured MCP servers to RPent tools over Streamable HTTP."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import AsyncIterator, Callable
from concurrent.futures import wait
from contextlib import ExitStack, asynccontextmanager
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from anyio import fail_after
from anyio.from_thread import start_blocking_portal
from jsonschema import Draft202012Validator
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rpent.tools.base import Tool, ToolResult

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


class McpServerConfig(BaseModel):
    """Connection and tool visibility settings for one MCP server."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    url: str
    enabled: bool = True
    enabled_tools: list[str] | None = None
    startup_timeout_sec: float = Field(default=5, gt=0, allow_inf_nan=False)
    tool_timeout_sec: float = Field(default=10, gt=0, allow_inf_nan=False)
    bearer_token_env_var: str | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
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
        return value


def load_mcp_servers(path: Path) -> dict[str, McpServerConfig]:
    """Read server entries from a TOML configuration file."""
    with path.expanduser().open("rb") as handle:
        config = tomllib.load(handle)
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        raise ValueError(f"{path} must contain an [mcp_servers] table")
    for name in servers:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"invalid MCP server name {name!r}")
    return {
        name: McpServerConfig.model_validate(server) for name, server in servers.items()
    }


class McpServiceError(RuntimeError):
    """An MCP service could not be discovered or called."""


def _error_message(error: Exception) -> str:
    while getattr(error, "exceptions", ()):
        error = error.exceptions[0]
    return str(error) or type(error).__name__


@asynccontextmanager
async def _connect(
    config: McpServerConfig, headers: dict[str, str]
) -> AsyncIterator[tuple[ClientSession, dict[str, types.Tool]]]:
    timeout = httpx.Timeout(config.startup_timeout_sec, read=config.tool_timeout_sec)
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, trust_env=False
    ) as http:
        async with streamable_http_client(config.url, http_client=http) as streams:
            read, write, _ = streams
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
                                raise ValueError(
                                    "MCP server repeated a tool-list cursor"
                                )
                            seen_cursors.add(cursor)
                yield session, tools


class McpClient:
    """Own one MCP connection for synchronous toolkit or navigation callers."""

    def __init__(self, name: str, config: McpServerConfig) -> None:
        self.name = name
        self._timeout = config.tool_timeout_sec
        headers = {}
        if config.bearer_token_env_var is not None:
            token = os.environ.get(config.bearer_token_env_var)
            if not token:
                raise McpServiceError(
                    f"MCP server {name!r} requires {config.bearer_token_env_var}"
                )
            headers["Authorization"] = f"Bearer {token}"
        try:
            with ExitStack() as connections:
                self._portal = connections.enter_context(start_blocking_portal())
                context = self._portal.wrap_async_context_manager(
                    _connect(config, headers)
                )
                self._session, self.tools = connections.enter_context(context)
                self._connections = connections.pop_all()
        except Exception as exc:
            raise McpServiceError(
                f"MCP server {name!r} startup failed: {_error_message(exc)}"
            ) from exc

    async def _call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            with fail_after(self._timeout):
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
    ) -> types.CallToolResult:
        """Call a discovered tool once within the configured deadline."""
        if name not in self.tools:
            raise McpServiceError(f"MCP server {self.name!r} does not export {name!r}")
        call = self._portal.start_task_soon(self._call, name, arguments)
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


def _tool_name(server: str, remote_name: str) -> str:
    name = f"mcp__{server}__{remote_name}"
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    if len(name) > 64 or normalized != name:
        return normalized[:54] + "__" + sha256(name.encode()).hexdigest()[:8]
    return name


def _invoke(
    client: McpClient,
    name: str,
    check_cancelled: Callable[[], None],
    /,
    **arguments: Any,
) -> ToolResult:
    try:
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
        elif isinstance(block, types.ImageContent) and block.mimeType == "image/png":
            images.append(base64.b64decode(block.data, validate=True))
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
    for name, config in servers.items():
        if not config.enabled or config.enabled_tools == []:
            continue
        client = McpClient(name, config)
        connections.callback(client.close)
        selected = (
            client.tools if config.enabled_tools is None else config.enabled_tools
        )
        for raw_name in selected:
            definition = client.tools.get(raw_name)
            if definition is None:
                raise ValueError(f"MCP server {name!r} does not export {raw_name!r}")
            Draft202012Validator.check_schema(definition.inputSchema)
            declarations.append(
                Tool(
                    name=_tool_name(name, raw_name),
                    description=definition.description or raw_name,
                    args_schema=None,
                    handler=partial(_invoke, client, raw_name, check_cancelled),
                    _input_schema=definition.inputSchema,
                    capture_state=False,
                )
            )
    return declarations

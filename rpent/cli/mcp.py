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

"""Manage configured MCP servers without starting a robot task."""

from __future__ import annotations

import argparse
import fcntl
import os
import stat
from collections.abc import MutableMapping
from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.toml_document import TOMLDocument

from rpent.tools.mcp_client import (
    McpServerConfig,
    McpServiceError,
    discover_mcp_tools,
    load_mcp_servers,
    parse_mcp_servers,
    validate_mcp_server_name,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rpent mcp", description=__doc__)
    parser.add_argument(
        "--mcp-config",
        type=Path,
        help="TOML config path (defaults to RPENT_MCP_CONFIG or ./mcp.toml).",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser(
        "add",
        help="Add a server by HTTP URL or stdio command.",
        usage="%(prog)s NAME [options] (--url URL | -- COMMAND [ARGS ...])",
    )
    add.add_argument("name")
    add.add_argument("--url", help="Streamable HTTP endpoint.")
    add.add_argument(
        "--bearer-token-env-var", help="Environment variable for HTTP authentication."
    )
    add.add_argument(
        "--cwd",
        help="Stdio working directory, relative to the config file or absolute.",
    )
    add.add_argument(
        "--env-from", action="append", help="Pass a variable to the stdio process."
    )
    add.add_argument(
        "--tool",
        action="append",
        help="Expose this remote tool. Repeat to select more.",
    )
    add.add_argument("--startup-timeout-sec", type=float)
    add.add_argument("--tool-timeout-sec", type=float)
    actions.add_parser("list", help="List configured servers.")
    actions.add_parser(
        "test", help="Connect to an enabled server and discover its tools."
    ).add_argument("name")
    actions.add_parser("remove", help="Remove a server configuration.").add_argument(
        "name"
    )
    return parser


def _server_from_args(args: argparse.Namespace, command: list[str]) -> McpServerConfig:
    if (args.url is not None) == bool(command):
        raise ValueError("provide either --url URL or -- COMMAND [ARGS ...]")
    options = {
        name: value
        for name in ("startup_timeout_sec", "tool_timeout_sec")
        if (value := getattr(args, name)) is not None
    }
    return McpServerConfig(
        transport="stdio" if command else "streamable-http",
        url=args.url,
        command=command[0] if command else None,
        args=command[1:],
        cwd=args.cwd,
        env_from=args.env_from or [],
        enabled_tools=args.tool,
        bearer_token_env_var=args.bearer_token_env_var,
        **options,
    )


def _write_config(path: Path, document: TOMLDocument) -> None:
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            if path.exists():
                os.fchmod(handle.fileno(), stat.S_IMODE(path.stat().st_mode))
            handle.write(document.as_string())
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _edit_config(path: Path, name: str, server: McpServerConfig | None) -> None:
    if server is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    # Lock the directory so concurrent edits use the same lock after atomic saves.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        document = (
            tomlkit.parse(path.read_text(encoding="utf-8"))
            if path.exists()
            else tomlkit.document()
        )
        entries = document.get("mcp_servers")
        if entries is None:
            entries = tomlkit.table()
            document["mcp_servers"] = entries
        if not isinstance(entries, MutableMapping):
            raise ValueError(f"{path} must contain an [mcp_servers] table")
        if server is None:
            if name not in entries:
                raise ValueError(f"unknown MCP server {name!r} in {path}")
            del entries[name]
            if not entries:
                document["mcp_servers"] = tomlkit.table()
        else:
            if name in entries:
                raise ValueError(f"MCP server {name!r} already exists in {path}")
            entry = server.model_dump(exclude_defaults=True, exclude_none=True)
            entry["transport"] = server.transport
            entries[name] = entry
        parse_mcp_servers(document.unwrap(), path)
        _write_config(path, document)
    finally:
        os.close(directory)


def _test_server(name: str, path: Path) -> None:
    server = load_mcp_servers(path).get(name)
    if server is None:
        raise ValueError(f"unknown MCP server {name!r} in {path}")
    if not server.enabled or server.enabled_tools == []:
        raise ValueError(f"MCP server {name!r} has no enabled tools")
    with ExitStack() as connections:
        tools = discover_mcp_tools({name: server}, connections, lambda: None)
    print(f"Connected to {name} ({server.transport})")
    for tool in tools:
        print(tool.name)


def main(argv: list[str]) -> int:
    """Handle MCP management commands and return the CLI exit status."""
    parser = _parser()
    separator = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:separator])
    command = argv[separator + 1 :]
    if separator < len(argv) and (args.action != "add" or not command):
        parser.error("-- COMMAND [ARGS ...] is only valid with mcp add")
    path = args.mcp_config or Path(os.environ.get("RPENT_MCP_CONFIG") or "mcp.toml")
    try:
        path = path.expanduser().resolve()
        if args.action == "list":
            servers = load_mcp_servers(path) if path.exists() else {}
            if not servers:
                print("No MCP servers configured.")
            for name, server in sorted(servers.items()):
                state = "enabled" if server.enabled else "disabled"
                print(f"{name}\t{server.transport}\t{state}")
        else:
            name = validate_mcp_server_name(args.name)
            if args.action == "test":
                _test_server(name, path)
            else:
                server = (
                    _server_from_args(args, command) if args.action == "add" else None
                )
                _edit_config(path, name, server)
                verb = "Added" if server is not None else "Removed"
                print(f"{verb} MCP server {name!r} in {path}")
    except (McpServiceError, OSError, TOMLKitError, ValueError) as exc:
        parser.error(str(exc))
    return 0

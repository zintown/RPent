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

"""Check public MCP config commands and their effect on later tasks."""

import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from rpent.cli import main as cli
from rpent.cli.mcp import main
from rpent.tools.mcp_client import load_mcp_servers


def test_mcp_commands_preserve_toml_and_discover_configured_stdio_tools(
    tmp_path, monkeypatch, capsys
):
    working_directory = tmp_path / "local MCP"
    working_directory.mkdir()
    script = working_directory / "tool.py"
    script.write_text(
        """
import sys
from mcp.server.fastmcp import FastMCP

assert sys.argv[1:] == ["--label", "two words"]
mcp = FastMCP("offline")

@mcp.tool()
def add(a: int, b: int) -> int:
    return a + b

mcp.run(transport="stdio")
"""
    )
    path = tmp_path / "mcp.toml"
    original = '# keep this comment\n[other]\nsetting = "keep"\n'
    path.write_text(original)
    monkeypatch.setenv("RPENT_MCP_CONFIG", str(path))

    def invoke(*args):
        monkeypatch.setattr(sys, "argv", ["rpent", "mcp", *args])
        return cli.main()

    assert invoke("add", "remote", "--url", "http://127.0.0.1:8790/mcp") == 0
    assert (
        invoke(
            "add",
            "calculator",
            "--cwd",
            working_directory.name,
            "--tool",
            "add",
            "--",
            sys.executable,
            script.name,
            "--label",
            "two words",
        )
        == 0
    )
    assert invoke("list") == 0
    assert "calculator\tstdio\tenabled" in capsys.readouterr().out
    assert invoke("test", "calculator") == 0
    assert "mcp__calculator__add" in capsys.readouterr().out
    assert load_mcp_servers(path)["calculator"].cwd == str(working_directory)
    assert invoke("remove", "calculator") == 0
    assert invoke("remove", "remote") == 0
    assert load_mcp_servers(path) == {}
    assert original in path.read_text()


def test_mcp_rejects_mixed_transport_options_without_writing(tmp_path):
    path = tmp_path / "mcp.toml"
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--mcp-config",
                str(path),
                "add",
                "ambiguous",
                "--url",
                "http://127.0.0.1:8790/mcp",
                "--",
                sys.executable,
                "calculator.py",
            ]
        )
    assert error.value.code == 2
    assert not path.exists()


def test_mcp_checks_show_status_and_never_call_actions(tmp_path, capsys):
    (tmp_path / "probe.py").write_text(
        """
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("probe")

@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_pose() -> dict:
    return {"ok": True, "age_s": 0.1, "map_version": "1"}

@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_navigation_status() -> dict:
    return {"ready": False, "reason": "map_mismatch"}

@server.tool()
def follow_plan() -> str:
    Path("followed").touch()
    return "followed"

server.run(transport="stdio")
"""
    )
    path = tmp_path / "mcp.toml"
    options = ["--mcp-config", str(path)]
    assert (
        main([*options, "add", "go2", "--cwd", ".", "--", sys.executable, "probe.py"])
        == 0
    )
    assert main([*options, "test", "go2", "--check", "get_pose"]) == 0
    assert '"map_version": "1"' in capsys.readouterr().out
    assert main([*options, "test", "go2", "--check", "get_navigation_status"]) == 1
    assert "map_mismatch" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        main([*options, "test", "go2", "--check", "follow_plan"])
    assert error.value.code == 2
    assert not (tmp_path / "followed").exists()


def test_concurrent_additions_preserve_each_server(tmp_path):
    path = tmp_path / "mcp.toml"

    def add(name):
        return main(
            [
                "--mcp-config",
                str(path),
                "add",
                name,
                "--url",
                "http://127.0.0.1:8790/mcp",
            ]
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(add, ["first", "second"])) == [0, 0]
    assert set(load_mcp_servers(path)) == {"first", "second"}

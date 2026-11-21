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

"""Serve robot discovery and task execution through Streamable HTTP MCP."""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from rpent.mcp.task.config import Environment, load_deployment
from rpent.mcp.task.runner import TaskRunner
from rpent.mcp.task.tools import create_task_mcp
from rpent.utils.config import get_repo_root


def create_app(
    *,
    repo_root: Path | None = None,
    jobs_dir: Path | None = None,
    token: str | None = None,
    mcp_config: Path | None = None,
    robot_id: str | None = None,
    environment: Environment | None = None,
    mcp_allowed_hosts: list[str] | None = None,
) -> FastAPI:
    """Build the MCP task service; own its runner and session manager in the lifespan."""
    root = repo_root or get_repo_root()
    path = mcp_config or Path(os.environ.get("RPENT_MCP_CONFIG") or "mcp.toml")
    deployment = load_deployment(
        root / path.expanduser(),
        robot_id=robot_id,
        environment=environment,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runner = TaskRunner(root, jobs_dir or root / "logs" / "http-runner", deployment)
        app.state.runner = runner
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            await asyncio.to_thread(runner.close)

    app = FastAPI(
        title="RPent robot task runner",
        lifespan=lifespan,
        openapi_url=None,
    )
    mcp_server = create_task_mcp(
        lambda: app.state.runner, deployment=deployment, allowed_hosts=mcp_allowed_hosts
    )
    mcp_app = mcp_server.streamable_http_app()

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return app.state.runner.health()

    async def authorized_mcp(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and token is not None:
            authorization = Headers(scope=scope).get("authorization", "")
            scheme, _, supplied = authorization.partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(
                supplied.encode(), token.encode()
            ):
                response = JSONResponse(
                    {"detail": "valid Bearer token required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        if scope["type"] == "http" and scope["path"].endswith("/mcp/"):
            scope = {**scope, "path": scope["path"][:-1]}
        await mcp_app(scope, receive, send)

    app.mount("/", authorized_mcp)
    return app


def main(argv: list[str] | None = None) -> None:
    """Serve one worker on the RPent host, using server-side configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8780, type=int)
    parser.add_argument(
        "--mcp-config",
        type=Path,
        help="Deployment TOML. Defaults to RPENT_MCP_CONFIG or <repo>/mcp.toml.",
    )
    parser.add_argument("--robot-id", help="Override the deployment's robot ID.")
    parser.add_argument(
        "--environment",
        choices=("simulation", "real"),
        help="Override the environment.",
    )
    parser.add_argument(
        "--mcp-allowed-host",
        action="append",
        help="Additional trusted Host header, e.g. go2-01.lan:8780; repeatable.",
    )
    parser.add_argument(
        "--jobs-dir", type=Path, help="default: <repo>/logs/http-runner"
    )
    args = parser.parse_args(argv)
    token = os.environ.get("RPENT_RUNNER_TOKEN") or None
    if args.host not in {"127.0.0.1", "::1", "localhost"} and token is None:
        parser.error("set RPENT_RUNNER_TOKEN before binding a non-loopback address")
    try:
        app = create_app(
            jobs_dir=args.jobs_dir,
            token=token,
            mcp_config=args.mcp_config,
            robot_id=args.robot_id,
            environment=args.environment,
            mcp_allowed_hosts=args.mcp_allowed_host,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

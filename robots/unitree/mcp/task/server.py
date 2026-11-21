# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# https://www.apache.org/licenses/LICENSE-2.0

"""Compatibility entry point for Unitree task MCP deployments."""

from typing import Any

from fastapi import FastAPI

from rpent.mcp.task.server import create_app as create_task_app
from rpent.mcp.task.server import main as serve_tasks


def create_app(**kwargs: Any) -> FastAPI:
    """Load a Unitree deployment, including earlier service-only TOML files."""
    return create_task_app(legacy_unitree=True, **kwargs)


def main(argv: list[str] | None = None) -> None:
    """Serve Unitree deployments through the shared task entry point."""
    serve_tasks(argv, legacy_unitree=True)


if __name__ == "__main__":
    main()

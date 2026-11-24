#!/usr/bin/env bash
# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

set -euo pipefail

if [[ $# -eq 1 && "$1" == '--help' ]]; then
    cat <<'HELP'
用法：bash run_rpent.sh '自然语言任务'

先准备 .venv。脚本自动加载仓库的 .env.local，执行调度端指定的任务。

可选环境变量：
  RPENT_CONFIG_FILE               自定义 Bash 配置文件
  RPENT_MCP_CONFIG                MCP 配置文件，默认仓库根目录的 mcp.toml
  RPENT_ROBOT_ADAPTER             手动覆盖部署文件所选的机器人适配器
  RPENT_VENV_DIR                  已安装 RPent 的虚拟环境目录
  RPENT_MODEL                     模型，默认 openai:deepseek-flash
  RPENT_PLANNER_TIMEOUT_S         模型会话预算，默认 900 秒
  RPENT_MAX_TURNS                 模型请求上限，默认 16
  RPENT_RUNS_DIR                  运行输出父目录，默认 logs
  RPENT_MEMORY_DIR                可选独立本地记忆目录

Go2 仿真使用说明见 robots/unitree/README.md。
HELP
    exit 0
fi

if [[ $# -ne 1 || -z "${1//[[:space:]]/}" ]]; then
    echo "用法：bash run_rpent.sh '自然语言任务'" >&2
    exit 2
fi

rpent_repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
rpent_config_file="${RPENT_CONFIG_FILE:-$rpent_repo_dir/.env.local}"
rpent_dispatch_adapter="${RPENT_DISPATCH_ADAPTER:-}"
rpent_dispatch_config="${RPENT_DISPATCH_MCP_CONFIG:-}"
rpent_dispatch_output="${RPENT_DISPATCH_OUTPUT_DIR:-}"
if [[ -f "$rpent_config_file" ]]; then
    source "$rpent_config_file"
elif [[ -n "${RPENT_CONFIG_FILE:-}" ]]; then
    echo "RPent configuration file not found: $rpent_config_file" >&2
    exit 2
fi

rpent_python="${RPENT_VENV_DIR:-$rpent_repo_dir/.venv}/bin/python"
if [[ ! -x "$rpent_python" ]]; then
    echo "RPent Python not found: $rpent_python; set RPENT_VENV_DIR to the RPent environment." >&2
    exit 2
fi

# 从其他目录调用时，也以仓库为基准解析路径。
rpent_python=$(cd -- "$(dirname -- "$rpent_python")" && pwd)/python
cd -- "$rpent_repo_dir"
rpent_run_dir="$rpent_dispatch_output"
if [[ -z "$rpent_run_dir" ]]; then
    mkdir -p -- "${RPENT_RUNS_DIR:-logs}"
    rpent_run_dir=$(mktemp -d "${RPENT_RUNS_DIR:-logs}/rpend-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
fi
export RPENT_TASK_OUTPUT_DIR="$(cd -- "$rpent_run_dir" && pwd)"
echo "RPent output: $rpent_run_dir"

rpent_memory_args=()
if [[ -n "${RPENT_MEMORY_DIR:-}" ]]; then
    rpent_memory_args=(--memory-dir "$RPENT_MEMORY_DIR")
fi
rpent_robot_args=()
rpent_adapter="${rpent_dispatch_adapter:-${RPENT_ROBOT_ADAPTER:-}}"
if [[ -n "$rpent_adapter" ]]; then
    rpent_robot_args=(--robot "$rpent_adapter")
fi
exec "$rpent_python" -m rpent.cli.main \
    "${rpent_robot_args[@]}" \
    --mcp-config "${rpent_dispatch_config:-${RPENT_MCP_CONFIG:-$rpent_repo_dir/mcp.toml}}" \
    "${rpent_memory_args[@]}" \
    --planner api --model "${RPENT_MODEL:-openai:deepseek-flash}" \
    --task="$1" \
    --planner-timeout-s "${RPENT_PLANNER_TIMEOUT_S:-900}" \
    --max-turns "${RPENT_MAX_TURNS:-16}" \
    --output-dir "$rpent_run_dir"

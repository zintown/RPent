#!/usr/bin/env bash
# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

set -euo pipefail

if [[ $# -eq 1 && "$1" == '--help' ]]; then
    cat <<'HELP'
用法：bash run_rpent.sh TASK_ID '自然语言任务'

先准备 .venv。脚本自动加载仓库的 .env.local，执行调度端指定的任务。

可选环境变量：
  RPENT_CONFIG_FILE               自定义 Bash 配置文件
  RPENT_MCP_CONFIG                外部 MCP 服务 TOML 配置文件
  RPENT_VENV_DIR                  已安装 RPent 的虚拟环境目录
  RPENT_UNITREE_MODE              mujoco（默认）、mcp 或 real
  RPENT_REAL_ENDPOINT             真机通信服务根地址，例如 http://127.0.0.1:9001
  RPENT_ROBOT_INDEX               real 模式所用的本体编号
  RPENT_REAL_ACTION_TIMEOUT_S     真机单次移动或抓拍预算，默认 180 秒
  RPENT_NAV_ENDPOINT              A* 服务根地址
  RPENT_SIM_ENDPOINT              MuJoCo 控制服务根地址
  RPENT_VLN_ENDPOINT              可选 LightNav WebSocket 地址
  RPENT_OBSERVATION_ENDPOINT      VLN 第一视角观测服务根地址
  RPENT_VLN_TIMEOUT_S             单次 VLN 预算，默认 120 秒
  RPENT_VLN_INFERENCE_TIMEOUT_S   单次模型请求预算，默认 3 秒
  RPENT_MODEL                     模型，默认 openai:deepseek-flash
  RPENT_NAVIGATION_TIMEOUT_S      单次行走预算，默认 300 秒
  RPENT_PLANNER_TIMEOUT_S         模型会话预算，默认 900 秒
  RPENT_MAX_TURNS                 模型请求上限，默认 16
  RPENT_RUNS_DIR                  运行输出父目录，默认 logs
  RPENT_MEMORY_DIR                可选独立本地记忆目录

仿真说明：robots/unitree/MUJOCO_USAGE.zh-CN.md。
真机说明：robots/unitree/REAL_USAGE.zh-CN.md。
HELP
    exit 0
fi

if [[ $# -ne 2 || -z "${1//[[:space:]]/}" || -z "${2//[[:space:]]/}" ]]; then
    echo "用法：bash run_rpent.sh TASK_ID '自然语言任务'" >&2
    exit 2
fi

rpent_repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
rpent_unitree_mode="${RPENT_UNITREE_MODE:-}"
rpent_config_file="${RPENT_CONFIG_FILE:-$rpent_repo_dir/.env.local}"
if [[ -f "$rpent_config_file" ]]; then
    source "$rpent_config_file"
elif [[ -n "${RPENT_CONFIG_FILE:-}" ]]; then
    echo "RPent configuration file not found: $rpent_config_file" >&2
    exit 2
fi

rpent_unitree_mode="${rpent_unitree_mode:-${RPENT_UNITREE_MODE:-mujoco}}"
rpent_mode_args=(--unitree-mode "$rpent_unitree_mode")
case "$rpent_unitree_mode" in
    mujoco)
        rpent_mode_args+=(
            --nav-endpoint "${RPENT_NAV_ENDPOINT:-http://192.168.104.191:8765}"
            --sim-endpoint "${RPENT_SIM_ENDPOINT:-http://192.168.104.191:8770}"
            --navigation-timeout-s "${RPENT_NAVIGATION_TIMEOUT_S:-300}"
        )
        if [[ -n "${RPENT_VLN_ENDPOINT:-}" ]]; then
            rpent_mode_args+=(
                --vln-endpoint "$RPENT_VLN_ENDPOINT"
                --observation-endpoint "${RPENT_OBSERVATION_ENDPOINT:-http://127.0.0.1:8771}"
                --vln-timeout-s "${RPENT_VLN_TIMEOUT_S:-120}"
                --vln-inference-timeout-s "${RPENT_VLN_INFERENCE_TIMEOUT_S:-3}"
            )
        fi
        ;;
    mcp) ;;
    real)
        if [[ -z "${RPENT_REAL_ENDPOINT:-}" || ! "${RPENT_ROBOT_INDEX:-}" =~ ^[1-9][0-9]*$ ]]; then
            echo 'Real mode requires RPENT_REAL_ENDPOINT and a positive RPENT_ROBOT_INDEX.' >&2
            exit 2
        fi
        rpent_mode_args+=(
            --real-endpoint "$RPENT_REAL_ENDPOINT"
            --robot-index "$RPENT_ROBOT_INDEX"
            --real-action-timeout-s "${RPENT_REAL_ACTION_TIMEOUT_S:-180}"
        )
        ;;
    *) echo 'RPENT_UNITREE_MODE must be mujoco, mcp or real.' >&2; exit 2 ;;
esac

rpent_python="${RPENT_VENV_DIR:-$rpent_repo_dir/.venv}/bin/python"
if [[ ! -x "$rpent_python" ]]; then
    echo "RPent Python not found: $rpent_python; set RPENT_VENV_DIR to the RPent environment." >&2
    exit 2
fi

# 从其他目录调用时，也以仓库为基准解析路径。
rpent_python=$(cd -- "$(dirname -- "$rpent_python")" && pwd)/python
cd -- "$rpent_repo_dir"
"$rpent_python" -c 'import sys; from robots.unitree.task_server import validate_task_id; validate_task_id(sys.argv[1])' "$1"
mkdir -p -- "${RPENT_RUNS_DIR:-logs}"
rpent_run_dir=$(mktemp -d "${RPENT_RUNS_DIR:-logs}/rpend-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
echo "RPent output: $rpent_run_dir"

rpent_memory_args=()
if [[ -n "${RPENT_MEMORY_DIR:-}" ]]; then
    rpent_memory_args=(--memory-dir "$RPENT_MEMORY_DIR")
fi
rpent_mcp_args=()
if [[ -n "${RPENT_MCP_CONFIG:-}" ]]; then
    rpent_mcp_args+=(--mcp-config "$RPENT_MCP_CONFIG")
fi

exec "$rpent_python" -m rpent.cli.main \
    --robot unitree "${rpent_mode_args[@]}" \
    "${rpent_memory_args[@]}" \
    "${rpent_mcp_args[@]}" \
    --planner api --model "${RPENT_MODEL:-openai:deepseek-flash}" \
    --task="$2" \
    --planner-timeout-s "${RPENT_PLANNER_TIMEOUT_S:-900}" \
    --max-turns "${RPENT_MAX_TURNS:-16}" \
    --output-dir "$rpent_run_dir"

# MuJoCo 仿真导航

RPent 通过 A* MCP 查询点位和规划路径，再把 `plan_id` 交给 MuJoCo MCP。MuJoCo MCP 按编号取回原始路径，调用仿真服务执行并验收到达。

## 启动 A* 和 MuJoCo MCP

启动现有 A* 和 MuJoCo HTTP 服务，确认两者的 `map_id`、`map_version` 一致，且仿真已经运行。以下示例使用本机的 `8765` 和 `8770`。在 RPent 仓库目录打开两个终端，使用已安装 RPent 的 Python 环境分别启动。

```bash
../.venv/bin/python -m robots.unitree.astar_mcp \
  --nav-endpoint http://127.0.0.1:8765 --port 8790
```

```bash
../.venv/bin/python -m robots.unitree.mujoco_mcp \
  --sim-endpoint http://127.0.0.1:8770 \
  --astar-mcp-url http://127.0.0.1:8790/mcp --port 8791
```

在 RPent 运行机器上保存 `mcp.toml`。

```toml
[mcp_servers.astar]
url = "http://127.0.0.1:8790/mcp"
enabled_tools = ["list_waypoints", "plan_route"]

[mcp_servers.mujoco]
url = "http://127.0.0.1:8791/mcp"
enabled_tools = ["get_pose", "follow_plan", "get_walk_status"]
tool_timeout_sec = 150
```

`follow_plan` 等待实际行走完成。它的默认行走预算为 120 秒，可用 MuJoCo MCP 的 `--walk-timeout-s` 调整。同步把 `tool_timeout_sec` 设得比行走预算加清理时间更长。

## 提交一条导航任务

在 RPent 仓库目录配置私有 `.env.local` 中的模型凭据，字段见 [配置示例](../../.env.example)。按实际场景替换示例点位。

```bash
export RPENT_VENV_DIR="$(cd .. && pwd)/.venv"
export RPENT_UNITREE_MODE=mcp
export RPENT_MCP_CONFIG="$PWD/mcp.toml"
bash run_rpent.sh "mcp-$(date -u +%Y%m%dT%H%M%SZ)" '前往厕所通道并确认到达'
```

直接运行 CLI 时可用 `--unitree-mode mcp --mcp-config "$PWD/mcp.toml"`。DSH 通过 [RPent 任务 MCP](TASK_MCP.zh-CN.md)提交任务。为 RPent 增加其他 MCP 服务时，可在配置中加入对应的 `[mcp_servers.<服务名>]`，详见[规划器配置](../../docs/source-zh/rst_source/guides/configure_planner.rst)。

| 服务能力 | 输入与结果 |
| --- | --- |
| A* `list_waypoints` | 返回地图版本、实时点位名称、编号和站位。 |
| A* `plan_route` | 输入起点 `{x,y,yaw}` 和一个目标名称、编号或地图坐标，返回 `plan_id` 和路径摘要。 |
| A* `get_plan` | 按 `plan_id` 读取原始规划 JSON。MuJoCo MCP 用它传递完整路径。 |
| MuJoCo `get_pose` | 返回实时地图位姿。 |
| MuJoCo `follow_plan` | 输入 `plan_id`，成功时返回行走编号、最终位姿和到达证据。 |
| MuJoCo `get_walk_status` | 返回当前或最近一次行走的状态。 |

`legs[].path` 是每段路径的完整 `[x,y]` 点列，`legs[].headings` 对应相邻点之间的方向。位置为米，角度为弧度。每个目标从实时位姿重新规划。服务重启后也需重新规划。

## 验收和排查

任务目录中的 `transcript_unitree_*.json` 记录 MCP 调用和 `finish`，`run.log` 记录运行信息。业务成功需要 `finish.status=success` 和 `follow_plan.navigation_completed=true`。仿真到达校验要求回执 `reached=true`、未摔倒，位置误差不超过 0.25 米；指定朝向时，朝向误差不超过 0.2 弧度。

在转接服务所在机器上可直接检查底层接口。

```bash
curl --noproxy '*' -fsS http://127.0.0.1:8765/health
curl --noproxy '*' -fsS http://127.0.0.1:8765/waypoints
curl --noproxy '*' -fsS http://127.0.0.1:8770/pose
curl --noproxy '*' -fsS http://127.0.0.1:8770/walk
```

地图不一致或机器人摔倒时先处理仿真状态。动作回执丢失时，先查行走编号和原执行状态，再决定是否启动下一次任务。

## LightNav 和直接 HTTP 仿真

LightNav 使用 `mujoco` 模式，启动方式见 [LightNav 接入](VLN_USAGE.zh-CN.md)。`scripts/test_rpent_local.sh` 可检查 RPent 到 A* 和 MuJoCo 的直接 HTTP 仿真链路。

需要检查 HTTP 端点和启动仿真器时，在当前 Bash 终端设置 `RPENT_NAV_ENDPOINT` 和 `RPENT_SIM_ENDPOINT` 后执行准备脚本。

```bash
source .env.local
source robots/unitree/prepare_mujoco.sh --skip-install --start-simulator
```

脚本复用 `RPENT_VENV_DIR` 指定的环境，检查模型、地图、控制器、位姿和点位。`--start-simulator` 只在仿真器未运行时调用启动接口。

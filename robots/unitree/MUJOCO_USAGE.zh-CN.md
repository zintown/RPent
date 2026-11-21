# MuJoCo 仿真服务

MuJoCo 在 `mujoco_sim` 仓库中独立运行，提供 Go2 仿真运动、位姿和第一视角。默认 MCP 地址为 `http://127.0.0.1:8791/mcp`。

## 启动仿真

按 `mujoco_sim/README.zh-CN.md` 准备仿真环境。从项目根目录运行下列命令。示例使用 `room1110/2026-11-11.2`。仿真已运行时，直接启动 MCP 即可。

```bash
cd mujoco_sim
export ROBOT_MAP=room1110/2026-11-11.2
export ROBOT_PYTHON="$PWD/../.venv/bin/python"
bash scripts/run_robot_mujoco.sh start
"$ROBOT_PYTHON" -m mujoco_mcp.server
```

MuJoCo MCP 默认连接本机 8770 端口的仿真控制服务，可用 `--sim-endpoint` 修改。仿真第一视角由 8771 端口提供，可在浏览器打开 `http://127.0.0.1:8771/camera/front`。

## 在 RPent 中接入

在 [mcp.toml](../../mcp.toml) 的 `[mcp_servers.mujoco]` 配置仿真 MCP 地址。从 RPent 根目录检查 MuJoCo 的工具列表。

```bash
../.venv/bin/rpent mcp --mcp-config mcp.toml test mujoco
```

| 工具 | 输入和输出 |
| --- | --- |
| `get_pose()` | 返回 Go2 的实时地图位姿和跌倒状态 |
| `get_walk_status()` | 返回当前或最近一次行走的状态 |
| `follow_plan(plan_id)` | 取回指定路线并执行，返回行走编号、最终位姿和到达证据 |
| `get_navigation_status()` | 检查控制器占用和规划地图版本是否匹配 |

MuJoCo 的位姿查询和第一视角可独立使用。执行地图路线时，MuJoCo 按 `plan_id` 从 [A* MCP](ASTAR_USAGE.zh-CN.md) 取回规划。两个服务需使用相同地图 ID 和版本。A* 地址变更时，使用 MuJoCo 的 `--astar-mcp-url` 指定实际地址。

MuJoCo 的默认行走预算是 120 秒，可用 `--walk-timeout-s` 调整。`tool_timeout_sec` 需覆盖行走和停车清理。在 MuJoCo 的 `enabled_tools` 中加入 `preview_plan` 可预演单目标路线，预演失败后重新规划。

## 验收和排查

在任务目录的 `transcript_unitree_*.json` 中核对 `finish.status=success` 和 `follow_plan` 的 `navigation_completed=true`。到达验收要求 `reached=true`、Go2 站立且位置误差不超过 0.25 米。指定朝向时，朝向误差需在 0.2 弧度以内。

在 MuJoCo 所在主机检查仿真状态。

```bash
curl --noproxy '*' -fsS http://127.0.0.1:8770/health
curl --noproxy '*' -fsS http://127.0.0.1:8770/pose
curl --noproxy '*' -fsS http://127.0.0.1:8770/walk
```

仿真启停和状态查询由 `mujoco_sim/scripts/run_robot_mujoco.sh` 管理。回执丢失时，先根据行走编号核对执行和停车状态。

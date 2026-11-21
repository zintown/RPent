# A* 路径规划服务

A* 在 `a` 仓库中独立运行，提供地图点位查询和路径规划。默认 MCP 地址为 `http://127.0.0.1:8790/mcp`。

## 启动服务

将地图包放在 `a/slam_map_demo/maps/<地图>/<版本>`。地图包包含地图信息、站位表和碰撞数据。以下命令从项目根目录开始，使用 `room1110/2026-11-11.2` 地图。

```bash
cd a
export ROBOT_MAP=room1110/2026-11-11.2
export ROBOT_PYTHON="$PWD/../.venv/bin/python"
bash scripts/run_robot_astar.sh start
"$ROBOT_PYTHON" -m astar_mcp.server
```

A* MCP 默认读取本机 8765 端口的规划服务。原生服务已运行时，直接启动 MCP 即可。用 `--nav-endpoint` 指定规划服务地址，用 `--port` 指定 MCP 端口。

## 在 RPent 中接入

在 [mcp.toml](../../mcp.toml) 的 `[mcp_servers.astar]` 配置 A* 地址。从 RPent 根目录检查 A* 的工具列表。

```bash
../.venv/bin/rpent mcp --mcp-config mcp.toml test astar
```

## 规划接口

默认配置为 RPent 提供点位查询和路径规划。执行端可通过 `get_plan` 获取完整路线。

| 工具 | 输入和输出 |
| --- | --- |
| `list_waypoints()` | 返回当前地图版本、点位名称、编号和站位 |
| `plan_route(start, goal)` | 输入地图起点 `{x,y,yaw?}` 和一个目标名称、编号或坐标，返回 `plan_id` 及路径摘要 |
| `get_plan(plan_id)` | 按编号读取完整规划 |

`get_plan` 返回的 `legs[].path` 包含完整的 `[x,y]` 点列。`legs[].headings` 是相邻点之间的方向。长度用米，角度用弧度。地图改变或 A* MCP 重启后重新规划。

## 状态和排查

在 A* 所在主机检查地图信息和实时点位。

```bash
curl --noproxy '*' -fsS http://127.0.0.1:8765/health
curl --noproxy '*' -fsS http://127.0.0.1:8765/waypoints
```

在 `a` 仓库执行 `bash scripts/run_robot_astar.sh status` 查看规划服务状态，或执行 `bash scripts/run_robot_astar.sh stop` 停止它。前台 MCP 进程可用 `Ctrl+C` 停止。

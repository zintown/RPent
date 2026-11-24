# A* 路径规划服务

A* 负责地图点位查询和路径规划。RPent 默认使用远程端点 `http://192.168.104.191:8769/mcp`。

## 在 RPent 中接入

在 [mcp.toml](../../mcp.toml) 的 `[mcp_servers.astar]` 配置地址，`[service_adapters]` 为它选择 `robot_astar` 适配器。从 RPent 根目录检查工具发现。

```bash
.venv/bin/rpent mcp --mcp-config mcp.toml test astar
```

## 规划接口

| 工具 | 输入和输出 |
| --- | --- |
| `service_health()` | 返回 `map_id`、`map_version` 和点位数量 |
| `plan_route(start_x, start_y, goals, start_yaw?)` | 输入地图起点和有序目标列表，返回完整规划 |

目标可以是名称、点位 ID 或坐标对象。规划的 `legs[].path` 是分段的 `[x, y]` 点列，分段目标包含站位和朝向。长度用米，角度用弧度。RPent 核对规划和执行端的地图版本，并把 A* 回执中的 `structuredContent` 完整交给仿真。

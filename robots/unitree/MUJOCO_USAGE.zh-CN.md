# MuJoCo 仿真服务

RPent 默认连接 `http://192.168.104.191:8772/mcp` 上的 `robot-sim`。仿真启停由服务方或操作者管理，网页推流地址见 `sim_status` 返回的 `stream.url`。

## 在 RPent 中接入

在 [mcp.toml](../../mcp.toml) 的 `[mcp_servers.sim]` 配置地址，`[service_adapters]` 为它选择 `robot_sim` 适配器。从 RPent 根目录检查工具发现。

```bash
.venv/bin/rpent mcp --mcp-config mcp.toml test sim
```

| 工具 | 输入和输出 |
| --- | --- |
| `sim_status()` | 检查地图、仿真器、位姿和控制器占用 |
| `get_pose()` | 返回 Go2 的实时地图位姿和跌倒状态 |
| `walk_start(plan)` | 接收完整规划，在后台行走并返回 `walk_id` |
| `walk_status()` | 返回当前或最近一次行走的状态和逐段到达结果 |
| `walk_stop()` | 停止当前行走 |

## 验收和排查

RPent 轮询 `walk_status`，核对同一 `walk_id` 已结束、每段都返回 `reached = true`、且没有跌倒。单次 MCP 请求预算由 `tool_timeout_sec` 设置，RPent 等待一次行走的预算为 600 秒。任务目录的 `navigation-*.json` 记录提交和终态，DSH 可从 `evidence.navigation` 读取结果。

`walk_stop()` 操作的是当前行走。每个仿真实例由一个 RPent 控制，取消时先核对行走编号，再发出停止请求并确认终态。提交或停车状态不明时，任务进入 `requires_attention`。

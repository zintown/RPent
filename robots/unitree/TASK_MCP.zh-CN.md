# DSH 调度 Go2

DSH 通过 RPent 的 `/mcp` 查询 Go2 的能力并下发整条任务。RPent 调用 A* 规划路线，并通过 MuJoCo 执行。

## 启动服务

先安装 RPent 并确认能访问配置中的远程 A* 和 MuJoCo，再在 RPent 根目录运行任务 MCP。下列命令让同机的 DSH 连接 RPent。远程仿真器运行后，导航能力才可能就绪。 [RPent 安装](../../rpent/mcp/task/README.zh-CN.md#安装依赖) · [A* 路径规划](ASTAR_USAGE.zh-CN.md) · [MuJoCo 仿真](MUJOCO_USAGE.zh-CN.md)

```bash
set -a
source .env.local
set +a
export RPENT_VENV_DIR="$PWD/.venv"

"$RPENT_VENV_DIR/bin/python" -m rpent.mcp.task.server \
  --mcp-config "${RPENT_MCP_CONFIG:-$PWD/mcp.toml}" \
  --host 127.0.0.1 --port 8780 \
  --jobs-dir output/task-mcp/go2-01
```

任务服务使用配置文件中的 `[robot]` 读取机器人身份和环境。使用自定义配置时，以 [mcp.toml](../../mcp.toml) 的 `[robot]` 为例，并在修改配置后重启任务服务。跨主机测试可继续使用此仿真配置，启动参数见 [RPent 任务 MCP](../../rpent/mcp/task/README.zh-CN.md#配置和启动)。

## 接入 Go2 真机 MCP

Go2 的下游服务实现 MCP 端点以及位姿、导航和执行回执工具。RPent 使用 [mcp.go2.toml](../../mcp.go2.toml) 连接该端点，示例配置开放位姿和导航状态查询。将 `[mcp_servers.go2]` 的 `url` 填为 Go2 MCP 地址，按下游实际提供的工具设置 `enabled_tools`。

先在 RPent 环境中查询位姿和导航状态。

```bash
rpent mcp --mcp-config mcp.go2.toml test go2 --check get_pose
rpent mcp --mcp-config mcp.go2.toml test go2 --check get_navigation_status
```

`get_pose` 在定位有效时返回 `ok = true`、`frame_id = "map"`、米制坐标 `x` 和 `y`、弧度航向 `yaw`、`age_s`、`map_id` 和 `map_version`。`get_navigation_status` 返回 `ready`，不可执行时使用 `reason` 说明原因。两个工具都应声明 `readOnlyHint = true`。定位有效时，DSH 从 `get_robot_info` 看到 `localization` 能力为 `ready`。

接入导航后，在真机配置中登记 A* 和 `map_navigation` 能力，并将下游提供的 `follow_plan` 和 `get_walk_status` 加入工具白名单。Go2 在执行路线前确认定位、地图版本和本体控制器状态，并在执行结束后确认到达和停稳。

Go2 提供按运行 ID 查询的回执时，在 RPent 配置 `run_report_tool = "get_run_report"`。RPent 按任务 MCP 的通用回执格式读取执行证据。Go2 在回执中使用 `requires_attention` 表示停车状态待核对，使用 `verification_required` 表示需要独立验收。[RPent 回执格式](../../rpent/mcp/task/README.zh-CN.md#dsh-调用)

## 连接和调用

DSH 与 RPent 同机时，通过 `--patch` 加载 `apps/cli/config/examples/rpent/local.cordis.yml`，连接 `http://127.0.0.1:8780/mcp`。跨主机时，加载同目录的 `remote.cordis.yml` 并通过 `DSH_RPENT_REMOTE_URL` 指定 RPent 的完整 `/mcp` 地址。需要 Bearer 鉴权时，按 DSH 的 `apps/cli/config/examples/rpent/USAGE.zh-CN.md` 添加认证请求头。

DSH 先调用 `get_robot_info()`，确认节点为 `idle` 且所需能力为 `ready`。再通过 `run_task(instruction)` 提交完整指令并等待终态。下面的示例只查询位姿。

```json
{"instruction": "查询仿真 Go2 的当前位置，并报告位姿"}
```

每个 RPent 节点同时执行一条任务。两份 DSH 配置的默认工具调用超时为 960 秒，覆盖 RPent 默认的 900 秒规划预算和停车清理时间。修改 RPent 预算后，可通过 `DSH_MCP_TOOL_TIMEOUT_MS` 调整对应的调用超时。

## 查看结果和日志

`run_task` 返回任务终态、摘要、错误、执行证据和本机的 `output_dir`。Go2 的地图导航证据位于 `evidence.navigation`。

| 终态 | 含义 |
| --- | --- |
| `succeeded` | 任务执行成功 |
| `stopped` | 任务已取消并结束 |
| `requires_attention` | 执行或停车状态仍需核对 |
| `verification_required` | 执行已结束，仍需补充到达或验收证据 |

任务目录中的 `job.json` 保存终态和证据，`stdout.log` 保存执行日志。通过 MCP 取消后，可以从这两个文件核对停车结果。`GET /healthz` 返回节点占用和待核对状态。完整接口和配置字段见 [RPent 任务 MCP](../../rpent/mcp/task/README.zh-CN.md)。

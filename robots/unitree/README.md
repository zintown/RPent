# Unitree Go2 仿真

RPent 通过 MCP 调用 A* 的地图规划和 MuJoCo 的运动控制。终端可以直接提交自然语言任务，DSH 可以通过 RPent 任务 MCP 下发同样的任务。

| 使用场景 | 指南 |
| --- | --- |
| 查询地图点位和规划路径 | [A* 路径规划服务](ASTAR_USAGE.zh-CN.md) |
| 启动 Go2 仿真和第一视角 | [MuJoCo 仿真服务](MUJOCO_USAGE.zh-CN.md) |
| 用 DSH 查询机器人能力和下发任务 | [DSH 调度 Go2](TASK_MCP.zh-CN.md) |

## 运行准备

先安装 RPent，并在仓库根目录配置私有 `.env.local`。A* 和 MuJoCo 在各自仓库安装和启动。 [RPent 安装](../../rpent/mcp/task/README.zh-CN.md#安装依赖) · [环境变量示例](../../.env.example)

在 RPent 根目录运行一条只读任务，检查当前位姿。

```bash
bash run_rpent.sh '查询 Go2 当前地图位姿'
```

脚本加载 `.env.local` 和默认的 [mcp.toml](../../mcp.toml)。用 `RPENT_MCP_CONFIG` 选择其他配置。直接运行 RPent 命令行时使用 `--mcp-config`。

## 提交地图导航任务

分别启动 A* 和 MuJoCo 并确认地图版本一致。RPent 先读取仿真位姿，再让 A* 规划路径，最后将 `plan_id` 交给 MuJoCo 执行。目标名称和编号以当前地图的点位表为准。

```bash
bash run_rpent.sh '前往厕所通道（wp_02），确认到达并停稳'
```

同一条指令也可以[通过 DSH 提交](TASK_MCP.zh-CN.md)。

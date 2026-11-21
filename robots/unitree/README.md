# Unitree Go2 仿真

RPent 通过 MCP 调用 A* 的地图规划、MuJoCo 的运动控制和 LightNav 的视觉导航。终端可以直接提交自然语言任务，DSH 可以通过 RPent 任务 MCP 下发同样的任务。

| 使用场景 | 指南 |
| --- | --- |
| 查询地图点位和规划路径 | [A* 路径规划服务](ASTAR_USAGE.zh-CN.md) |
| 启动 Go2 仿真和第一视角 | [MuJoCo 仿真服务](MUJOCO_USAGE.zh-CN.md) |
| 根据 Go2 第一视角执行局部导航 | [LightNav 视觉导航](VLN_USAGE.zh-CN.md) |
| 用 DSH 查询机器人能力和下发任务 | [DSH 调度 Go2](TASK_MCP.zh-CN.md) |

## 运行准备

本地示例使用并列的 `a`、`mujoco_sim`、`lightnav` 和 `rpent` 目录，Python 环境位于它们的父目录 `.venv`。按导航指南启动对应服务，并参照 [环境变量示例](../../.env.example) 在 RPent 根目录配置私有 `.env.local`。

在 RPent 根目录运行一条只读任务，检查当前位姿。

```bash
export RPENT_VENV_DIR="$(cd .. && pwd)/.venv"
bash run_rpent.sh '查询 Go2 当前地图位姿'
```

脚本加载 `.env.local` 和默认的 [mcp.toml](../../mcp.toml)。默认启用 A* 和 MuJoCo。使用视觉导航时启用 `[mcp_servers.vln]`。用 `RPENT_MCP_CONFIG` 选择其他配置。直接运行 RPent 命令行时使用 `--mcp-config`。

## 提交地图导航任务

分别启动 A* 和 MuJoCo 并确认地图版本一致。RPent 先读取仿真位姿，再让 A* 规划路径，最后将 `plan_id` 交给 MuJoCo 执行。目标名称和编号以当前地图的点位表为准。

```bash
bash run_rpent.sh '仅用 A* 前往厕所通道（wp_02），确认到达并停稳'
```

同一条指令也可以[通过 DSH 提交](TASK_MCP.zh-CN.md)。

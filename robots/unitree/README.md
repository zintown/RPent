# Unitree 接入

RPent 从 MCP 配置发现 Unitree 服务。任务通过同一个命令执行。

```bash
export RPENT_VENV_DIR="$(cd .. && pwd)/.venv"
bash run_rpent.sh '查询 Go2 当前位姿'
```

在 RPent 仓库根目录运行。脚本加载私有 `.env.local` 和默认的 `mcp.toml`。用 `RPENT_MCP_CONFIG` 选择其他 MCP 配置，直接运行 RPent CLI 时用 `--mcp-config` 指定配置。

| 服务 | MCP 入口 | 使用说明 |
| --- | --- | --- |
| A* | [mcp/astar/server.py](mcp/astar/server.py) | [地图规划](MUJOCO_USAGE.zh-CN.md) |
| MuJoCo | [mcp/mujoco/server.py](mcp/mujoco/server.py) | [仿真执行](MUJOCO_USAGE.zh-CN.md) |
| LightNav | [mcp/vln/server.py](mcp/vln/server.py) | [视觉导航](VLN_USAGE.zh-CN.md) |
| Go2 真机 | [mcp/real/server.py](mcp/real/server.py) | [真机接入](REAL_USAGE.zh-CN.md)、[A* 短路线测试](REAL_TEST_PLAN.zh-CN.md) |
| 任务调度 | [mcp/task/server.py](mcp/task/server.py) | [DSH 接入](TASK_MCP.zh-CN.md) |

A* 和 MuJoCo 转接服务使用 Streamable HTTP。LightNav 和 Go2 真机转接程序使用 stdio，由 RPent 为每条任务启动。它们各自连接已运行的推理、仿真或真机通信进程。

直接运行 RPent CLI 并启用上述 stdio 配置时，将 `RPENT_TASK_OUTPUT_DIR` 设为 `--output-dir` 对应的绝对路径。使用 `run_rpent.sh` 时脚本会自动设置。

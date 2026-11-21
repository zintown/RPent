# Unitree 接入

RPent 从 MCP 配置发现 Unitree 服务。任务通过同一个命令执行。

```bash
export RPENT_VENV_DIR="$(cd .. && pwd)/.venv"
bash run_rpent.sh '查询 Go2 当前位姿'
```

在 RPent 仓库根目录运行。脚本加载私有 `.env.local` 和默认的 `mcp.toml`。用 `RPENT_MCP_CONFIG` 选择其他 MCP 配置，直接运行 RPent CLI 时用 `--mcp-config` 指定配置。

| 服务 | 默认 MCP 地址 | 使用说明 |
| --- | --- | --- |
| A* | `http://127.0.0.1:8790/mcp` | [地图规划](MUJOCO_USAGE.zh-CN.md) |
| MuJoCo | `http://127.0.0.1:8791/mcp` | [仿真执行](MUJOCO_USAGE.zh-CN.md) |
| LightNav | `http://127.0.0.1:8792/mcp` | [视觉导航](VLN_USAGE.zh-CN.md) |
| RPent 任务入口 | `http://127.0.0.1:8780/mcp` | [DSH 接入](TASK_MCP.zh-CN.md) |

三个下游服务在各自仓库启动，RPent 根据 `mcp.toml` 通过 Streamable HTTP 发现和调用工具。默认启用 A* 和 MuJoCo，使用视觉导航时启用 `mcp_servers.vln`。

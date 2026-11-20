# Unitree 接入

| 模式 | 运行方式 | 使用说明 |
| --- | --- | --- |
| `mcp` | RPent 从配置发现 A* 和 MuJoCo MCP 服务，按计划编号执行地图导航 | [仿真导航](MUJOCO_USAGE.zh-CN.md) |
| `mujoco` | 直接连接 A* 和 MuJoCo，可启用 LightNav 图像导航 | [LightNav](VLN_USAGE.zh-CN.md) |
| `real` | 通过 Go2 通信服务的 HTTP `/call` 查询、相对移动、抓拍和停止 | [真机接入](REAL_USAGE.zh-CN.md)、[A* 真机验证](REAL_TEST_PLAN.zh-CN.md) |

直接运行 `rpent --robot unitree` 时默认使用 `mcp`，需提供 `--mcp-config`。`run_rpent.sh` 默认使用 `mujoco` 模式，设置 `RPENT_UNITREE_MODE=mcp` 可切换到 MCP 仿真。命令行任务需在终端中执行，任务服务会自动为子进程分配终端。

[RPent 任务 MCP 服务](TASK_MCP.zh-CN.md)负责接收外部中控发来的完整任务，以及提供查询、日志和取消接口。中控在自己的服务器上配置 RPent 的网络地址和任务令牌。RPent 根据本机选择的模式调用导航或真机服务。

## 代码入口

| 文件 | 职责 |
| --- | --- |
| [robot_spec.py](robot_spec.py)、[prompt_bundle.py](prompt_bundle.py) | 运行模式、参数与模型提示 |
| [mcp/astar/server.py](mcp/astar/server.py)、[mcp/mujoco/server.py](mcp/mujoco/server.py) | A* 规划与 MuJoCo 执行的 MCP 入口 |
| [navigation_client.py](navigation_client.py)、[simulation_client.py](simulation_client.py) | 地图、路径、位姿和仿真执行接口 |
| [mujoco_toolkit.py](mujoco_toolkit.py)、[vln_navigation.py](vln_navigation.py) | 仿真执行、LightNav 推理和图像航点控制 |
| [mcp/task/server.py](mcp/task/server.py)、[mcp/task/tools.py](mcp/task/tools.py)、[mcp/task/runner.py](mcp/task/runner.py) | DSH 任务入口、调度工具、子进程与执行记录 |
| [real_client.py](real_client.py)、[real_toolkit.py](real_toolkit.py) | 真机通信、异步请求状态和停车确认 |

在仓库根目录运行离线测试。

```bash
python -m pytest tests/unit_tests/robots/unitree -q
```

# Unitree Go2 仿真

RPent 通过 MCP 调用 A* 的地图规划和 MuJoCo 的运动控制。终端可以直接提交自然语言任务，DSH 可以通过 RPent 任务 MCP 下发同样的任务。

| 使用场景 | 指南 |
| --- | --- |
| 查询地图点位和规划路径 | [A* 路径规划服务](ASTAR_USAGE.zh-CN.md) |
| 查询远程仿真状态和推流地址 | [MuJoCo 仿真服务](MUJOCO_USAGE.zh-CN.md) |
| 用 DSH 查询机器人能力和下发任务 | [DSH 调度 Go2](TASK_MCP.zh-CN.md) |

## 运行准备

先安装 RPent，并在仓库根目录配置私有 `.env.local`。默认部署连接 `192.168.104.191` 上的 A* 和 MuJoCo MCP。RPent 主机需要能访问该地址的 `8769` 和 `8772` 端口。 [RPent 安装](../../rpent/mcp/task/README.zh-CN.md#安装依赖) · [环境变量示例](../../.env.example)

在 RPent 根目录运行一条只读任务，检查远程导航是否就绪。

```bash
bash run_rpent.sh '查询远程 Go2 仿真是否可以导航'
```

脚本加载 `.env.local` 和默认的 [mcp.toml](../../mcp.toml)。用 `RPENT_MCP_CONFIG` 选择其他部署。直接运行 RPent 命令行时使用 `--mcp-config`。适配器由部署文件的 `[robot].adapter` 选择。

## 提交地图导航任务

远程仿真器运行且与 A* 使用相同地图版本时，可以提交导航任务。RPent 读取仿真位姿后调用 A*，把完整规划交给 MuJoCo，再按行走编号确认逐段到达。目标名称和编号以远程地图为准。

```bash
bash run_rpent.sh '前往广告牌，确认到达'
```

同一条指令也可以[通过 DSH 提交](TASK_MCP.zh-CN.md)。

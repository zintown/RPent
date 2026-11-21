# RPent 任务 MCP

Unitree 使用 RPent 的通用任务 MCP。DSH 通过 `get_robot_info` 选择机器人，再调用 `run_task` 下发完整的自然语言指令。接入方式和能力配置见 [RPent 任务 MCP](../../rpent/mcp/task/README.zh-CN.md)。

## 启动服务

在 RPent 仓库目录执行。示例绑定一台仿真 Go2，跨机器连接时，将监听地址和可信 Host 换成现场的局域网地址。

```bash
set -a
source .env.local
set +a
export RPENT_VENV_DIR="$(cd .. && pwd)/.venv"

"$RPENT_VENV_DIR/bin/python" -m rpent.mcp.task.server \
  --mcp-config "$PWD/mcp.toml" \
  --host 192.168.10.21 --port 8780 \
  --mcp-allowed-host go2-01.lan:8780 \
  --robot-id go2-01 \
  --jobs-dir output/task-mcp/jobs
```

私有 `.env.local` 配置模型凭据和 `RPENT_RUNNER_TOKEN`。本机连接可将监听地址设为 `127.0.0.1`。 [仿真](MUJOCO_USAGE.zh-CN.md) · [LightNav](VLN_USAGE.zh-CN.md)

`python -m robots.unitree.mcp.task.server` 也可以启动通用服务。原来只包含 `[mcp_servers]` 的配置可继续使用这个入口。为 DSH 提供环境与能力信息时，按以上各部署文档补充相应配置表。

## 连接和调用

在客户端填写 Streamable HTTP 地址 `http://go2-01.lan:8780/mcp`。通过 `Authorization: Bearer <令牌>` 传递与服务端相同的 `RPENT_RUNNER_TOKEN`。跨机器访问时，将 URL 中的主机和端口加入 `--mcp-allowed-host`。

`run_task` 的参数示例

```json
{"instruction": "查询仿真 Go2 的当前位置，并报告位姿"}
```

Unitree 的导航和 VLN 证据通过 `evidence.navigation` 返回，原调用方仍可读取 `navigation` 字段。`succeeded` 表示执行成功。`verification_required` 表示仍需验收，`requires_attention` 表示需要核对仿真控制状态。`output_dir` 位于 RPent 主机。

同一服务同时执行一条指令。每次调用都会启动一次新执行。客户端通过 MCP 取消正在运行的调用时，RPent 会清理对应进程。`GET /healthz` 可检查 `busy` 和 `requires_attention`。

DSH 可在设置中按上述地址添加 MCP 服务，也可通过 `--patch` 加载普通客户端配置。将客户端单次工具调用超时设为大于 RPent 整任务预算的值。默认的 RPent 规划预算是 900 秒，DSH 可给这类调用配置 960,000 毫秒。

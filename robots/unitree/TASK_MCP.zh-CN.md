# RPent 任务 MCP 服务

每个 RPent runner 为一个机器人接收完整任务，调用本机配置的仿真或真机服务，并记录执行结果。DSH 在中控服务器运行，通过网络调用 RPent 的 `/mcp/`。

## 在 RPent 机器上启动

以下命令在 RPent 仓库目录执行。将监听地址和可信 Host 换成这台机器的局域网地址和中控实际访问的域名或 IP。为每台机器人配置唯一 `--robot-id`。在私有 `.env.local` 中设置模型凭据和 `RPENT_RUNNER_TOKEN`。

```bash
set -a
source .env.local
set +a

export RPENT_VENV_DIR="$(cd .. && pwd)/.venv"
export RPENT_UNITREE_MODE=mcp
export RPENT_MCP_CONFIG="$PWD/mcp.toml"

"$RPENT_VENV_DIR/bin/python" -m robots.unitree.mcp.task.server \
  --host 192.168.10.21 --port 8780 \
  --mcp-allowed-host go2-01.lan:8780 \
  --robot-id go2-01 \
  --jobs-dir output/task-mcp/jobs
```

此例使用 [MCP 仿真配置](MUJOCO_USAGE.zh-CN.md)。使用 LightNav 或连接真机时，选择对应的 [LightNav 配置](VLN_USAGE.zh-CN.md)或[真机配置](REAL_USAGE.zh-CN.md)。DSH 提供任务 ID，RPent 将记录保存到 `--jobs-dir`。沿用原目录可继续查询历史任务。

中控连接 `http://go2-01.lan:8780/mcp/`，并通过 `Authorization: Bearer <令牌>` 传递对应的 runner 令牌。使用 IP 访问时，将其连同端口填入 `--mcp-allowed-host`。如需支持两种地址，可重复指定此参数。`GET /healthz` 检查 runner 状态。

## 调度接口

| MCP 能力 | 用途 |
| --- | --- |
| `list_robots()` | 获取当前端点的机器人 ID、运行中的任务和待核对任务。 |
| `submit_robot_task(robot_id, task_id, task_name)` | 提交完整自然语言任务，立即返回 `created` 和任务记录。 |
| `get_robot_task(robot_id, task_id)` | 查询任务状态、`finish`、输出目录和可用的执行证据。 |
| `wait_robot_task(robot_id, task_id, timeout_s)` | 等待最多 30 秒。返回 `terminal=false` 时继续查询。 |
| `get_robot_logs(robot_id, task_id, tail_bytes)` | 读取任务日志尾部，最多 65,536 字节。 |
| `stop_robot_task(robot_id, task_id)` | 请求停止。继续查询终态和停车或待核对状态。 |

同一个 runner 同时执行一个任务。提交后用原任务 ID 查询，使用相同 ID 和完全相同的文本重新提交会返回原记录。中控断开时，已受理的任务继续执行。以业务状态 `succeeded` 和执行证据为准判断成功。

返回 `requires_attention` 或 `interrupted` 时，先核对原任务与机器人状态。连接返回 `401` 时检查两端令牌，返回 `421` 时检查访问地址和 `--mcp-allowed-host`。

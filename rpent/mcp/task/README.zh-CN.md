# RPent 任务 MCP

每台机器人上运行一个 RPent 任务服务。中心 DSH 按机器人地址接入其 Streamable HTTP `/mcp`。RPent 通过本地 MCP 配置发现和调用规划、定位和控制服务。

## 安装依赖

在 RPent 仓库根目录使用 Python 3.12 安装 RPent 和基础依赖。 [依赖清单](../../../requirements.txt)

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## 配置和启动

`mcp.toml` 同时声明机器人的身份、能力和 RPent 可用的下游服务。以下示例假设机械臂的 MCP 服务已经在本机 8792 端口运行。

```toml
[robot]
id = "arm-01"
type = "franka.panda"
environment = "real"
adapter = "mcp"

[mcp_servers.arm]
url = "http://127.0.0.1:8792/mcp"
enabled_tools = ["get_status", "grasp"]

[capabilities.grasp]
description = "抓取可定位的物品并确认抓取结果。"
tools = ["arm.grasp", "arm.get_status"]
checks = { "arm.get_status" = { ready = true } }
```

`id` 在 DSH 接入的机器人中保持唯一。`environment` 取 `real` 或 `simulation`。`adapter` 默认是 `mcp`。根目录的 [mcp.toml](../../../mcp.toml) 用通用模式连接远程 A* 和 MuJoCo。新增服务可直接加入 `[mcp_servers]`，RPent 读取服务公布的工具说明和参数定义。

`tools` 中的引用对应 `服务名.工具名`，这些工具需要对任务模型公开。`checks` 指定无参只读工具和预期返回值。只读属性可由服务的 `readOnlyHint` 声明，也可在部署的 `read_only_tools` 中声明。检查工具可独立于模型的工具列表。`checks` 的字段名可以用点号读取嵌套对象，例如 `"camera.ready" = true`。

`enabled_tools` 限定 RPent 可调用的业务工具，`model_tools` 进一步选择其中公开给模型的工具。`model_tools = []` 让服务仅供工作流和就绪检查使用。省略 `model_tools` 时，普通服务公开启用的工具，使用 `bindings` 的服务默认由工作流调用。例如在既有的 `[mcp_servers.sim]` 中设置 `model_tools = ["sim_status"]`，可把仿真查询直接提供给模型，导航工作流可继续调用启用的行走工具。

模型可以用 `mcp_call_with_refs` 将一次调用的结构化结果交给另一个 MCP 工具。例如，默认配置下可把 A* 的完整规划结果作为 MuJoCo 的 `plan` 参数。模型根据工具说明选择调用顺序，RPent 保留完整对象并按目标工具的参数定义校验。[结果引用示例](DOWNSTREAM_MCP.zh-CN.md#跨服务传递工具结果)

可选的 [remote_navigation 配置](../../../robots/remote_navigation/mcp.toml) 提供 `navigate` 工作流，通过 `bindings` 绑定定位、规划和执行服务。该工作流处理地图一致性、到达验证与取消时的停车。普通工具与工作流在同一任务内共享 MCP 服务会话。

在 RPent 仓库根目录启动。与 DSH 同机仿真时，使用随仓库提供的 `mcp.toml` 并监听 `127.0.0.1`。远程连接使用 RPent 所在机器的可达地址，同一套仿真配置也可用于验证远程连接。部署到真机时，配置文件需要对应的真机适配器和下游 MCP 服务。

以下是私网远程启动示例。将 IP、主机名和证书路径换成部署环境的值。

```bash
set -a
source .env.local
set +a
export RPENT_VENV_DIR="$PWD/.venv"

"$RPENT_VENV_DIR/bin/python" -m rpent.mcp.task.server \
  --mcp-config "$PWD/mcp.arm.local.toml" \
  --host 192.168.10.22 --port 8780 \
  --mcp-allowed-host arm-01.lan:8780 \
  --tls-certfile /path/to/server.crt --tls-keyfile /path/to/server.key \
  --no-token \
  --jobs-dir output/task-mcp/arm-01
```

私有 `.env.local` 配置模型凭据。本机监听默认可在未设置 `RPENT_RUNNER_TOKEN` 时接收请求。非回环地址默认要求 `RPENT_RUNNER_TOKEN`，也可在由私网或网关控制访问权限时显式传入 `--no-token`。该选项会关闭 MCP 的 Bearer 鉴权。可信 Host 包含客户端所用地址的主机和端口。`--mcp-config` 可由 `RPENT_MCP_CONFIG` 指定，默认读取仓库的 `mcp.toml`。修改部署配置后重启任务服务。每条新任务根据当前配置重新发现下游工具。

同时指定 `--tls-certfile` 和 `--tls-keyfile` 可启用 HTTPS。证书的备用名称须覆盖 DSH 访问时用的主机名或 IP。DSH 使用 Node.js 时，可在启动前通过 `NODE_EXTRA_CA_CERTS` 提供私有 CA 证书或自签名的服务端证书。

同一机器人持续使用同一个任务目录。每次执行在其中创建独立的 `run-*/output` 和日志。不同机器人在一台主机测试时，分别配置端口和任务目录。

## DSH 调用

为每个机器人添加一条 MCP 客户端配置，使用独立的 `serverName`。按服务的监听方式选择 `https://<机器人主机>:8780/mcp` 或 `http://<机器人主机>:8780/mcp`，并按部署设置 `Authorization: Bearer <令牌>`。DSH 通过标准 MCP 工具列表发现以下工具。

| 工具 | 行为 |
| --- | --- |
| `get_robot_info()` | 返回 `robot_id`、`robot_type`、`environment`、`state`、`capabilities` 和 `service_errors`。选取状态为 `idle` 且所需能力均为 `ready` 的机器人。 |
| `run_task(instruction)` | 传入完整自然语言任务。同一次调用等待执行结束，返回 `robot_id`、`status`、`summary`、`error`、`evidence` 和本体上的 `output_dir`。 |
| `list_navigation_targets()` | 默认 Go2 部署提供的只读查询。返回远程地图的固定站位和场景图，忙或待核对时也可调用。 |

通过 `[read_only_queries.<工具名>]` 公开地点清单等查询。每条配置包含 `description` 和一个或多个 `tools`，工具引用采用 `服务名.工具名`。RPent 校验下游工具的只读声明与无参调用条件。查询返回按工具引用组织的 `results` 和 `errors`，经 MCP 直接返回给 DSH。

`get_robot_info` 会在空闲时检查服务和能力。工具缺失或检查失败时，依赖该服务的能力为 `unavailable`。独立能力各自检查。缺少就绪检查时，能力为 `unverified`。MCP 连接及工具发现错误按服务记录在 `service_errors` 中。没有绑定工作流且没有发现可用模型工具时，节点状态为 `unavailable`。RPent 读取到仍需核对的历史任务时状态为 `requires_attention`。

每个节点同时执行一条指令。客户端通过 MCP 取消正在运行的调用，RPent 随即请求结束本次执行。停车证据不完整时，以 `requires_attention` 暂停接收新任务。需要独立验收的任务返回 `verification_required`。`GET /healthz` 可查看进程占用和待核对状态。客户端单次工具超时应大于该机器人配置的任务预算和清理时间。

支持查询执行回执的 HTTP 服务可配置 `run_report_tool = "get_run_report"`。RPent 在工具调用的 `_meta` 中附带内部执行标识 `rpent.dev/run-id`。服务用同一标识提供本次执行的只读报告，供 RPent 在成功、失败或取消后查询。报告的 `state` 为 `running`、`finished` 或 `missing`，可包含 `evidence`、`requires_attention`、`verification_required`、`error` 和 `stop_error`。DSH 仍通过 `run_task(instruction)` 提交任务。

服务已有动作提交和状态查询接口时，可用 [`action_status`](DOWNSTREAM_MCP.zh-CN.md#按动作编号核对状态) 声明动作 ID、终态及成功条件。默认 Go2 配置通过该方式核对 MuJoCo 行走结果。取消任务时，通用模式查询已提交动作的状态，停机仍待确认时报告 `requires_attention`。需要自动停止当前行走时使用 `remote_navigation` 配置。

## 添加本体服务

开发下游服务时，参照 [RPent 下游 MCP 接口规范](DOWNSTREAM_MCP.zh-CN.md)。

启动下游服务的 MCP 接口，并在该机器人的 `[mcp_servers]` 下登记连接信息。需要向 DSH 报告业务能力时，在 `[capabilities]` 下声明能力及只读检查。按需在 `enabled_tools` 和 `model_tools` 中指定工具。工具改名时更新配置中对应的显式引用。专用工作流可通过 `RobotSpec` 的 `task_execution.read_evidence` 提供验收证据。

仿真和真机使用同一套 MCP 接入配置。通过 `--mcp-config` 选择部署文件，由 `[robot].environment` 描述运行环境，由 `[mcp_servers]` 指定实际连接的下游端点。下游服务实现并执行工具，RPent 发现工具、派发调用并读取回执。[Go2 仿真和真机配置](../../../robots/unitree/TASK_MCP.zh-CN.md)

`rpent mcp --mcp-config DEPLOYMENT.toml test SERVER --check TOOL` 可以在同一次连接中发现工具并查询状态。可重复传入 `--check`。检查会调用已声明为只读、可无参执行的工具，并打印 JSON 结果。某项结果包含 `ok = false` 或 `ready = false` 时命令返回退出码 1。[Go2 真机示例](../../../robots/unitree/TASK_MCP.zh-CN.md#接入-go2-真机-mcp)

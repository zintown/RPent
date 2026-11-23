# RPent 下游 MCP 接口规范

本规范用于实现由 RPent 调用的 MCP 服务，适用于观测、规划、推理和动作执行。下游服务定义工具并负责实际操作，RPent 依据部署配置发现工具、编排调用和汇总证据。DSH 使用 RPent 提供的任务接口。[RPent 任务 MCP](README.zh-CN.md)

## 1. 基础接入

服务必须支持 MCP 初始化，在初始化响应中声明 `tools` 能力，并实现 `tools/list` 和 `tools/call`。客户端和服务端在初始化时协商协议版本。工具的调用名称和入参以该服务当次连接公布的定义为准。[MCP 生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) · [MCP 工具协议](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

| 运行方式 | RPent 配置 | 服务要求 |
| --- | --- | --- |
| 独立进程 | `transport = "streamable-http"` 和完整的 `url` | 提供 Streamable HTTP MCP 端点，通常为 `/mcp`。需要 Bearer 鉴权时，通过 `bearer_token_env_var` 指定凭据的环境变量名。 |
| RPent 启动的本地子进程 | `transport = "stdio"`、`command`，按需设置 `args`、`cwd` 和 `env_from` | 标准输入和标准输出传递 MCP 消息，日志写入标准错误。 |

配置 `run_report_tool` 的服务必须使用 Streamable HTTP。RPent 在任务收尾时建立新会话，向本次尝试过动作调用的服务查询回执。[RPent MCP 配置](../../tools/mcp_client.py) · [回执查询实现](reports.py) · [MCP 传输协议](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

## 2. 工具声明与结果

| MCP 字段 | 接入约定 |
| --- | --- |
| `name` | 在服务内唯一。工具名称应当稳定，并体现动作或查询对象。 |
| `description` | 说明工具何时可用、完成什么操作、返回什么结果。动作工具还应说明返回时动作处于哪个阶段。 |
| `inputSchema` | 必须是描述 JSON 对象入参的 JSON Schema。明确参数类型、必填项、单位和取值约束。RPent 使用 Draft 2020-12 验证任务模型传入的参数。 |
| `outputSchema` | 建议为结构化结果提供。声明后，服务必须返回符合声明的 `structuredContent`。 |
| `annotations.readOnlyHint` | 只读检查和回执工具必须声明为 `true`。动作工具应明确反映其副作用。 |

观察、查询和计算工具按业务需要定义返回字段。建议用 `structuredContent` 返回结构化对象。RPent 把它记为 `structured_content`，同时保留受支持的 `content`。模型可读文本用 `TextContent`，需要直接提供给 RPent 模型的图像用 `ImageContent` 的 `image/png` 格式。跨服务使用数据引用时，约定引用的来源、有效期及取回方式。[RPent 工具结果处理](../../tools/mcp_client.py) · [MCP 工具结果](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

请求格式错误或工具未知时，按 MCP 规范返回 JSON-RPC 错误。工具执行失败时，用工具结果的 `isError = true` 并在文本内容中说明原因。执行服务负责校验收到的参数和执行条件。涉及坐标、时间或外部标识时，在工具协议中明确坐标系、单位、时间基准和标识范围。[MCP 错误处理](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#error-handling)

更新工具时应保留现有入参和结果的含义。需要增加必填参数或改变字段含义时，应使用新工具名或版本化端点，并同步更新使用方配置。两个后端共用同一工具契约时，应保持参数、单位、结果和完成条件一致。

## 3. 能力与就绪检查

RPent 用 `[mcp_servers.<服务名>]` 登记下游服务。`enabled_tools` 填写下游原始工具名，用于控制任务模型能调用的工具。部署中的 `[capabilities]` 使用 `服务名.工具名` 声明一个能力需要哪些工具，可以引用多个服务。

部署把工具用于 `checks` 时，该工具必须可以用 `{}` 调用，具有 `readOnlyHint = true`，并返回 JSON 对象。只读检查应仅观察当前状态，允许重复调用。RPent 优先读 `structuredContent`，也接受单个文本块承载的 JSON 对象。配置中可用点号匹配嵌套字段。字段值和类型均需匹配。[能力检查实现](discovery.py) · [只读工具校验](../../tools/mcp_client.py)

以下是 `tools/list` 中一个检查工具的定义。

```json
{
  "name": "get_readiness",
  "description": "查询执行器是否可接收新动作。",
  "inputSchema": {"type": "object", "additionalProperties": false},
  "outputSchema": {
    "type": "object",
    "properties": {
      "ready": {"type": "boolean"},
      "reason": {"type": "string"}
    },
    "required": ["ready"]
  },
  "annotations": {"readOnlyHint": true}
}
```

`tools/call` 的 `result` 可以按下例返回，RPent 用 `structuredContent.ready` 和部署中配置的值比对。

```json
{
  "content": [{"type": "text", "text": "{\"ready\":true}"}],
  "structuredContent": {"ready": true},
  "isError": false
}
```

服务正常响应但暂时无法接收动作时，可返回 `{"ready": false, "reason": "控制器正在执行上一条动作"}`。能力所需工具和就绪检查均满足时，RPent 可以向上游报告 `ready`。工具缺失或检查失败时报告 `unavailable`，已发现所需工具但缺少就绪检查时报告 `unverified`。

## 4. 动作执行与回执

动作工具应说明一次调用的完成条件、最大执行时间和取消行为。建议一次调用返回本次动作的终态，并携带独立的 `action_id` 及结果证据。采用提交后查询的服务应提供按 `action_id` 查询状态的方法，并在工具描述中说明如何确认完成和发起取消。RPent 的 `tool_timeout_sec` 应覆盖工具的执行和清理预算。

执行服务收到取消后，应停止自己持有的动作并记录可确认的最终状态。执行结果无法确定时，调用方先根据状态或回执核对，再决定后续操作。服务可用请求去重标识避免重复提交，同一个 RPent 运行 ID 可以关联多个动作。[MCP 取消协议](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)

### RPent 回执约定

独立动作服务建议提供无参只读的 `get_run_report()`，在 RPent 中配置 `run_report_tool = "get_run_report"`。RPent 在被调度任务的工具请求元数据中带上 `rpent.dev/run-id`。服务按该标识记录结果，并支持使用同一标识从另一 MCP 会话查询。设置回执有效期时，应覆盖任务收尾和重连查询的时间窗口。[回执查询实现](reports.py)

回执查询请求示例中的 `run-id` 由 RPent 分配。

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "get_run_report",
    "arguments": {},
    "_meta": {"rpent.dev/run-id": "0123456789abcdef0123456789abcdef"}
  }
}
```

回执必须在 `structuredContent` 中返回对象。`state` 表示回执的处理状态，动作结果与确认依据由 `evidence` 和错误字段表达。回执缺失、读取失败、未收尾或标记了 `requires_attention` 时，RPent 会把任务标为 `requires_attention`。[任务收尾](runner.py)

| 字段 | 类型和含义 |
| --- | --- |
| `state` | 必填字符串。`running` 表示仍在处理，`finished` 表示回执已定稿，`missing` 表示找不到本次执行的记录。 |
| `evidence` | 可选对象。每个证据分组对应一个对象，例如 `action` 或 `navigation`。 |
| `requires_attention` | 可选布尔值。动作或停车状态需要人工核对时设为 `true`。 |
| `verification_required` | 可选布尔值。执行已收尾、任务仍需独立验收时设为 `true`。 |
| `error` | 可选字符串或 `null`。记录执行失败原因。 |
| `stop_error` | 可选字符串或 `null`。记录停止失败或无法确认停止的原因，并配合 `requires_attention` 表达状态。 |

以下是已确认动作完成和停止的 `structuredContent` 示例。

```json
{
  "state": "finished",
  "evidence": {
    "action": {
      "action_id": "action_001",
      "outcome": "succeeded",
      "stop_confirmed": true
    }
  },
  "requires_attention": false
}
```

## 5. 部署示例

以下为一个独立执行服务的 RPent 配置示例。工具名由该服务公布。`get_readiness` 供能力检查调用，`get_run_report` 供任务收尾调用。通过部署文件的 `[robot].environment` 区分 `simulation` 与 `real`，通过 `[mcp_servers]` 指定各自的后端。

```toml
[robot]
id = "example-01"
type = "example.robot"
environment = "simulation"
adapter = "mcp"

[mcp_servers.executor]
transport = "streamable-http"
url = "http://127.0.0.1:9000/mcp"
enabled_tools = ["execute_action", "get_action_status"]
tool_timeout_sec = 120
run_report_tool = "get_run_report"

[capabilities.operation]
description = "执行一次操作并确认结果。"
tools = ["executor.execute_action", "executor.get_action_status"]
checks = { "executor.get_readiness" = { ready = true } }
```

把示例存为部署文件并填入实际地址后，可从 RPent 环境查看工具和就绪结果。该诊断只调用指定的只读检查工具。

```bash
rpent mcp --mcp-config mcp.example.toml test executor --check get_readiness
```

命令会列出 RPent 已选用的工具，并打印检查结果。就绪检查返回 `ready = false` 或 `ok = false` 时退出码为 `1`。

接入验收应覆盖工具发现、有效入参和错误入参。配置了就绪检查时，验证就绪和暂不可用两种返回。配置了动作回执时，使用模拟服务验证正常完成、执行失败以及取消后按同一个运行 ID 查询回执。

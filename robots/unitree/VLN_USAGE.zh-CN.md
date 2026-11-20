# 在现有 Go2 仿真中运行 LightNav VLN

LightNav 根据第一视角图像和文字指令预测局部航点。RPent 工具内部持续取图、
推理并转发航点，由现有 Go2 仿真服务完成坐标转换、MPC 和步态控制。
A* 继续负责地图路径规划；同一台 Go2 每次只执行一种导航方式。

## 服务与环境

本机源码、权重、地图和 Python 环境均位于 `/home/yanlinjie/robot_project`。
四个组件共用 `.venv`；MuJoCo 保留 3.3.6，没有运行 LightNav 的 TurtleBot 演示。

| 服务 | 地址 | 职责 |
| --- | --- | --- |
| LightNav | `ws://127.0.0.1:8050` | `login/reset/next`，返回局部航点和模型 STOP |
| A* | `http://127.0.0.1:8765` | 点位查询、地图路径规划 |
| Go2 控制 | `http://127.0.0.1:8770` | `/walk` 或持续航点会话 |
| Go2 观测 | `http://127.0.0.1:8771` | `/observation`、`/camera/front` |
| RPent 任务 MCP | `http://127.0.0.1:8780/mcp/` | DSH 提交任务、查询进度、读取日志和取消 |

现有推理依赖、Go2 仿真和 A* 应已准备好。已有共享环境时，仅需注册本地包：

```bash
cd /home/yanlinjie/robot_project
uv pip install --offline --no-deps --no-build-isolation --python .venv/bin/python \
  --editable ./lightnav --editable ./rpent
uv pip check --python .venv/bin/python
```

RPent 客户端只需要 HTTP 和 WebSocket；`unitree-vln` extra 声明其可选 WebSocket
依赖。模型加载和 GPU 运算只发生在独立的 LightNav 服务进程。

## 启动与停止模型

在项目根目录执行：

```bash
.venv/bin/python lightnav/scripts/run_vln_service.py start
.venv/bin/python lightnav/scripts/run_vln_service.py status
```

默认使用物理 GPU 1、本地 `lightnav/checkpoints/LightNav-0`，监听回环地址 8050。
首次加载和预热可能需要数分钟；`status` 的 `ready:true` 表示预热后 WebSocket
探测成功。加载中返回退出码 1。日志为 `lightnav/output/vln-local/service.log`。
启动器强制使用离线权重，沿用上游 `local_serve.py` 的单进程 HashStore 初始化。

可在启动前设置 `VLN_CUDA_VISIBLE_DEVICES`、`VLN_MODEL_PATH`、`VLN_PORT` 和
`VLN_GPU_MEMORY_UTILIZATION`；默认显存比例为 0.4。选择其他端口时，状态检查及
RPent 的 `--vln-endpoint` 也要使用相同配置。停止命令只结束本启动器记录的模型进程：

```bash
.venv/bin/python lightnav/scripts/run_vln_service.py stop
```

仿真、A* 和 RPent 使用各自原有启停入口。模型服务关闭时，活动 VLN 工具会尝试
确认停车；不能确认时保留待核对状态，禁止自动重放运动。

## 通过 DSH 下发 VLN 任务

本机配置片段读取私有 `.env.local`，设置 `mujoco` 模式及 LightNav、观测和仿真地址。在 RPent 机器上执行。

```bash
cd /home/yanlinjie/robot_project/rpent
set -a
source robots/unitree/vln_local.env
set +a
../.venv/bin/python -m robots.unitree.mcp.task.server \
  --host 127.0.0.1 --port 8780 --robot-id go2-01 \
  --jobs-dir output/vln-integration/jobs
```

上例为本机监听。DSH 部署在另一台服务器时，按 [任务 MCP 服务](TASK_MCP.zh-CN.md)设置 RPent 的监听地址、可信 Host 和令牌。前台进程用 Ctrl+C 退出，退出前清理其拥有的任务和控制。

DSH 可以提交“使用 VLN 保持当前地图位置并确认停车”。RPent 读取当前位置，将它作为已知验收坐标，再调用 VLN。DSH 可通过同一 MCP 服务查询结果或请求停止。

默认调度先解析目标区域、物体和验收条件：已知地图站位使用 A*；
“前往某个房间寻找物体”拆成 A* 到房间、确认停车、VLN 局部搜索两个阶段。
例如“前往客厅找垃圾桶”先解析实时表中的完整名称“客厅”，再执行视觉搜索。
完整名称的唯一匹配优先于其他名称的部分匹配；客厅东区和客厅西区不会使
一个已有唯一完整名称的“客厅”变成歧义。确实无法解析区域时报告缺少信息。

明确要求“仅使用 VLN”时保留完整视觉路线；明确要求“仅使用 A*”时不会增加
视觉搜索。
传给 LightNav 的是当前 VLN 阶段的指令，省略已经完成的区域导航。
局部搜索通常先给 30 秒，且不超过配置的单次 VLN 上限；一次失败后不重复
提交同一阶段，也不切换算法绕过失败状态。

这些规则由 `prompt_bundle.py` 的 MuJoCo system prompt 提供。
每条新任务会启动新的 RPent 子进程并读取提示词，修改后对新任务生效；
正在运行的任务仍使用原提示词，8780 任务服务无需为此重启。
本地配置同时设置单段 A* 预算 300 秒、整任务规划预算 900 秒、最多 16 次模型请求；
这些值由每个新任务的启动脚本重新读取。VLN 单次最大预算仍为 120 秒。
房间边界监控和视觉目标的独立验收仍需要工具层支持；提示词中的局部搜索要求
不能保证机器人始终留在指定房间，地图站位验收也不能代替视觉识别证据。

也可在终端直接测试。RPent CLI 在 `--unitree-mode mujoco` 上增加以下参数。

```text
--vln-endpoint ws://127.0.0.1:8050
--observation-endpoint http://127.0.0.1:8771
--vln-timeout-s 120
--vln-inference-timeout-s 3
```

两个 endpoint 必须成对提供。省略时保留原 A* 工具集。

## 工具和闭环

| 工具 | 行为 |
| --- | --- |
| `run_vln_navigation(instruction, expected_goal=None, timeout_s=None)` | 一次调用内部循环，直到目标验收、模型 STOP、取消或失败 |
| `get_vln_status()` | 最近一次会话、预测数量、控制权和停车证据 |
| `stop_vln_navigation()` | 结束本工具拥有的控制会话并确认停车 |

`expected_goal` 是已知地图坐标 `{x,y,yaw?}`，不是给模型的局部航点。只能使用
任务或实时点位表提供的坐标；位置验收阈值为 0.25 米，可选朝向阈值为 0.2 弧度。
工具会在经过至少一轮真实预测后检查已知目标，并在停车后重新验收。

模型每次返回的全部 `[前向米, 左向米, 逆时针弧度]` 航点，与该次输入图像的
`stamp_ns`、`base_pose` 一起交给 `/control/trajectory`。RPent 不生成速度命令，
也不拿推理结束后的位姿替换采集位姿。默认按训练配置的 4 Hz 循环，每个连接
只保留一个在途推理请求；新任务使用新的连接并执行 reset。

轨迹有效期仍是采集后 2 秒。超时预测被丢弃，不修改时间戳续期；连续三次过期、
断连、MPC 错误、跌倒或任务超时均结束本次执行并尝试停车。旧会话不能停止新会话，
无法确认的创建或停车结果需要核对，不能重复提交运动。

模型 STOP、物理停车和目标验证分别记录：

- `model_stop`：模型是否输出 STOP。
- `stop_confirmed`：仿真是否确认站稳并释放控制权。
- `goal_verified`：是否通过独立地图位姿验收。

没有已知验收坐标时，模型 STOP 后会停车并返回 `verification_required`，
不会把它计入已验证目标或允许 RPent 报成功。上层任务应说明目标仍待验证。

## MCP 任务状态

活动工具持续写入 `navigation/vln/latest.json`。MCP 的 `get_robot_task` 在任务运行期间也可读取 `navigation.vln`。

```json
{
  "navigation": {
    "active_control_id": "control_...",
    "vln": {
      "episode_id": "vln_...",
      "state": "tracking",
      "predictions": 7,
      "accepted_references": 7,
      "latency_ms": 250.0,
      "capture_age_s": 0.31,
      "model_stop": false,
      "stop_confirmed": false,
      "goal_verified": false
    }
  }
}
```

`state` 可为 `connecting`、`starting_control`、`observing`、`inferencing`、
`tracking`、`waiting_fresh_prediction`、`stopping`、`finished`、
`verification_required`、`cancelled` 或 `failed`。`finished` 表示已知目标验收
及停车均通过。`control` 内有仿真步态、MPC 和实测运动反馈；`latest_frame` 是
本地诊断文件路径，浏览器实时画面仍使用 8771 的第一视角接口。

已停车但目标未验证时，任务终态为 `verification_required`。DSH 请求停止后继续查询，确认任务终态和 `stop_confirmed`。控制权或停车状态不明时，任务终态为 `requires_attention`，runner 等待核对后再接收新任务。

## 验证与限制

离线测试使用项目现有 pytest：

```bash
cd /home/yanlinjie/robot_project/rpent
../.venv/bin/python -m pytest tests/unit_tests/robots/unitree -q
```

实际联调需要已经启动的 A*、Go2 和 LightNav，且仿真与现有 RPent 任务均空闲。
以下检查通过任务 MCP 控制仿真 Go2。

```bash
RPENT_RUN_UNITREE_VLN=1 ../.venv/bin/python -m pytest \
  tests/e2e_tests/unitree/test_vln_policy_chain.py -q -s
```

它复用仓库的本地脚本规划器，只固定高层工具调用；LightNav 推理、Go2 图像、
航点转换、MPC 和 DDS 均实际运行。覆盖保持当前已知地图位置并停车，以及运动中
通过 MCP 取消后释放控制权。产物默认位于项目 `output/vln-integration/policy-chain/`。

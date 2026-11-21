# 在现有 Go2 仿真中运行 LightNav VLN

LightNav 根据第一视角图像和文字指令预测局部航点。VLN MCP 服务内部持续取图、
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
| RPent 任务 MCP | `http://127.0.0.1:8780/mcp` | DSH 调用 `run_task` 并接收最终结果 |

现有推理依赖、Go2 仿真和 A* 应已准备好。已有共享环境时，仅需注册本地包：

```bash
cd /home/yanlinjie/robot_project
uv pip install --offline --no-deps --no-build-isolation --python .venv/bin/python \
  --editable ./lightnav --editable ./rpent
uv pip check --python .venv/bin/python
```

LightNav 转接程序使用 HTTP 和 WebSocket。`unitree-vln` extra 声明其可选 WebSocket
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
VLN MCP 的 `--vln-endpoint` 也要使用相同配置。停止命令只结束本启动器记录的模型进程：

```bash
.venv/bin/python lightnav/scripts/run_vln_service.py stop
```

仿真、A* 和 RPent 使用各自原有启停入口。模型服务关闭时，活动 VLN 工具会尝试
确认停车；不能确认时保留待核对状态，禁止自动重放运动。

## 注册 LightNav MCP

先启动 [A* 和 MuJoCo MCP](MUJOCO_USAGE.zh-CN.md)。首次配置时在 RPent 仓库根目录执行下列命令。示例使用默认 LightNav、观测与仿真地址。

```bash
export RPENT_VENV_DIR="$(cd .. && pwd)/.venv"
export RPENT_MCP_CONFIG="$PWD/mcp.vln.local.toml"
export RPENT_RUNS_DIR="$PWD/output/vln-integration/runs"
cp -n mcp.toml "$RPENT_MCP_CONFIG"

"$RPENT_VENV_DIR/bin/rpent" mcp add vln \
  --tool-timeout-sec 150 --env-from RPENT_TASK_OUTPUT_DIR \
  -- "$RPENT_VENV_DIR/bin/python" -m robots.unitree.mcp.vln.server
```

底层地址变化时，在配置的启动参数中设置 `--vln-endpoint`、`--observation-endpoint` 或 `--sim-endpoint`。`--vln-timeout-s` 默认 120 秒，`--vln-inference-timeout-s` 默认 3 秒。MCP 的 `tool_timeout_sec` 应留出 episode 结束后的停车与清理时间。

在 `mcp.vln.local.toml` 中补充视觉导航的能力声明，供 DSH 调度时检查。

```toml
[capabilities.visual_navigation]
description = "使用第一视角定位目标并执行局部视觉导航。"
tools = ["vln.run_vln_navigation", "vln.get_vln_status", "vln.stop_vln_navigation"]
checks = { "vln.get_vln_readiness" = { ready = true } }
```

```bash
bash run_rpent.sh \
  '使用 VLN 保持当前地图位置并确认停车。先读取当前位置作为验收坐标。'
```

通过 DSH 提交时，用上述 `RPENT_MCP_CONFIG` 启动 [RPent 任务 MCP](TASK_MCP.zh-CN.md)。启动脚本为每条任务设置 `RPENT_TASK_OUTPUT_DIR`，供 stdio 服务写入执行记录。

已知坐标或地点可用 A* 到达。「前往客厅找垃圾桶」可以先用 A* 到已解析的客厅站位，停稳后再用 VLN 局部搜索。优先采用唯一的完整点位名称。明确指定仅用 A* 或仅用 VLN 时遵循该要求。局部搜索通常使用约 30 秒的预算，视觉指令只包含当前搜索阶段。房间范围与物体识别结果由实际观测验收。

## 工具和闭环

| 工具 | 行为 |
| --- | --- |
| `run_vln_navigation(instruction, expected_goal=None, timeout_s=None)` | 一次调用内部循环，直到目标验收、模型 STOP、取消或失败 |
| `get_vln_status()` | 最近一次会话、预测数量、控制权和停车证据 |
| `get_vln_readiness()` | 检查仿真控制器、第一视角观测和 LightNav 连接 |
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

活动工具持续写入 `navigation/vln/latest.json`。任务结束后，`run_task` 在 `evidence.navigation.vln` 中返回执行证据。

```json
{
  "evidence": {
    "navigation": {
      "active_control_id": null,
      "vln": {
        "episode_id": "vln_...",
        "state": "finished",
        "predictions": 7,
        "accepted_references": 7,
        "latency_ms": 250.0,
        "capture_age_s": 0.31,
        "model_stop": false,
        "stop_confirmed": true,
        "goal_verified": true
      }
    }
  }
}
```

`state` 可为 `connecting`、`starting_control`、`observing`、`inferencing`、
`tracking`、`waiting_fresh_prediction`、`stopping`、`finished`、
`verification_required`、`cancelled` 或 `failed`。`finished` 表示已知目标验收
及停车均通过。`control` 内有仿真步态、MPC 和实测运动反馈；`latest_frame` 是
本地诊断文件路径，第一视角画面可通过 8771 的观测接口查看。

已停车但目标未验证时，任务终态为 `verification_required`。取消 MCP 调用后，可以在 RPent 主机的 `navigation/vln/latest.json` 中核对停车。控制权或停车状态不明时，任务终态为 `requires_attention`，runner 等待核对后再接收新任务。

## 验证与限制

离线测试使用项目现有 pytest：

```bash
cd /home/yanlinjie/robot_project/rpent
../.venv/bin/python -m pytest tests/unit_tests/robots/unitree -q
```

实际联调需要已经启动的 A*、Go2 和 LightNav，且仿真与现有 RPent 任务均空闲。
以下检查通过任务 MCP 和 VLN MCP 控制仿真 Go2。

```bash
RPENT_RUN_UNITREE_VLN=1 ../.venv/bin/python -m pytest \
  tests/e2e_tests/unitree/test_vln_policy_chain.py -q -s
```

它复用仓库的本地脚本规划器，只固定高层工具调用；LightNav 推理、Go2 图像、
航点转换、MPC 和 DDS 均实际运行。覆盖保持当前已知地图位置并停车，以及运动中
通过 MCP 取消后释放控制权。产物默认位于项目 `output/vln-integration/policy-chain/`。

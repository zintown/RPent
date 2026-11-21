# LightNav 视觉导航

LightNav 从 Go2 第一视角和文字指令预测局部航点。视觉导航 MCP 在一次调用内完成取图、推理、航点下发和停车确认。Go2 的地图行走和视觉导航串行执行。

## 启动视觉导航服务

先启动[仿真和第一视角服务](MUJOCO_USAGE.zh-CN.md)。在 `lightnav` 仓库根目录启动模型。共享环境需安装 LightNav 及其 `mcp` 依赖，权重默认位于该仓库的 `checkpoints/LightNav-0`。安装、显卡和权重配置见 `lightnav/LOCAL_SERVICE.md`。

```bash
../.venv/bin/python scripts/run_vln_service.py start
../.venv/bin/python scripts/run_vln_service.py status
```

状态返回 `ready:true` 后，在同一仓库的新终端启动 MCP。

```bash
../.venv/bin/python -m lightnav.navigation_mcp.server
```

MCP 默认为 `http://127.0.0.1:8792/mcp`，连接 8050 的推理服务、8770 的仿真控制和 8771 的第一视角。对应地址可用 `--vln-endpoint`、`--sim-endpoint`、`--observation-endpoint` 设置。可通过 `http://127.0.0.1:8771/camera/front` 查看第一视角。

默认模型推理超时为 3 秒，视觉导航预算为 120 秒，可分别用 `--vln-inference-timeout-s` 和 `--vln-timeout-s` 调整。RPent 的 `tool_timeout_sec` 需覆盖视觉导航和停车清理。

任务结束并确认停车后，可在前台终端按 `Ctrl+C` 停止 MCP。在 LightNav 仓库根目录执行 `../.venv/bin/python scripts/run_vln_service.py stop` 停止模型。

## 在 RPent 中启用 VLN

将 [mcp.toml](../../mcp.toml) 的 `[mcp_servers.vln]` 中的 `enabled` 改为 `true`。可复制到 `mcp.vln.local.toml` 后修改，从而按需选择是否启用 VLN。从 RPent 根目录执行以下示例时，使用启用了 VLN 的配置。

```bash
export RPENT_VENV_DIR="$(cd .. && pwd)/.venv"
export RPENT_MCP_CONFIG="$PWD/mcp.vln.local.toml"
bash run_rpent.sh '先查询当前地图位姿，再用 VLN 保持原位并确认停车'
```

[通过 DSH 调用](TASK_MCP.zh-CN.md)时，任务服务也需加载这份配置。`get_robot_info()` 返回 `visual_navigation` 为 `ready` 后可调度视觉导航。

需要结合地图导航时，可先用 A* 到已确认的房间站位，停稳后再执行该处的视觉指令。局部搜索通常给约 30 秒。仅在已知视觉目标的地图站位时填写 `expected_goal`。原位停车可以用刚读取的位姿验收。

## 工具和验收

| 工具 | 用途 |
| --- | --- |
| `run_vln_navigation(instruction, expected_goal, timeout_s)` | 运行视觉导航直到目标验收、模型停车、取消或失败。后两个参数可省略 |
| `get_vln_readiness()` | 检查仿真控制、第一视角和模型连接 |
| `get_vln_status()` | 查询会话状态、预测次数和停车证据 |
| `stop_vln_navigation()` | 结束调用方持有的视觉导航会话并确认停车 |

每次推理输出的 `[前向米, 左向米, 逆时针弧度]` 航点与对应图像的 `stamp_ns` 和 `base_pose` 一起交给仿真控制。循环默认以 4 Hz 为目标，每次只保留一个在途推理请求。采集后超过 2 秒的预测会被丢弃，连续三次过期时结束会话并尝试停车。

已知目标在至少一次真实预测后检查，停车后再次用地图位姿验收。位置误差限 0.25 米，指定朝向时误差限 0.2 弧度。任务返回的 `evidence.navigation.vln` 包含三项独立证据。

| 字段 | 含义 |
| --- | --- |
| `model_stop` | 模型是否发出停车指令 |
| `stop_confirmed` | 仿真是否确认停稳并释放控制权 |
| `goal_verified` | 是否通过独立地图位姿验收 |

模型停车且停稳后，缺少已知验收位姿的任务报告 `verification_required`。取消后，RPent 通过 `get_run_report` 查询本次会话，将停车证据写入任务的 `job.json`。需要进一步排查时查看 LightNav 的 `output/vln-mcp`。

## 验证

在 Go2 仿真和 LightNav MCP 空闲时，从 RPent 根目录执行。测试覆盖原位停稳验收和运动中取消。上层工具顺序由本地脚本固定，第一视角、LightNav 推理和仿真控制实际运行。

```bash
RPENT_RUN_UNITREE_VLN=1 ../.venv/bin/python -m pytest \
  tests/e2e_tests/unitree/test_vln_policy_chain.py -q
```

默认记录保存到项目根目录的 `output/vln-integration/policy-chain`。

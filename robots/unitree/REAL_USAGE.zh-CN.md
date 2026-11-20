# 使用 RPent 连接 Go2 真机

`real` 模式通过通信服务的 `POST /call` 调用 Go2。RPent 和通信服务同机时使用 `http://127.0.0.1:9001`，分开部署时填写通信服务的局域网地址。通信服务负责连接本体。RPent 复用现有 Python 环境和模型配置。

`mujoco` 配置仍用于 A*、MuJoCo 和可选 LightNav。真机模式使用本体起始朝向下的相对坐标，提供动作执行和抓拍。要在真机上按 A* 地图路线导航，还需要同一地图中的实时定位和路径跟踪接口。当前抓拍返回本体路径和元信息，真机 VLN 还需要可读取的图像、拍摄时位姿和航点控制接口。

## 确认本体编号并提交只读任务

在服务器上进入 `robot_project/rpent`，先查当前连接的本体。将示例地址替换为现场通信服务地址。

```bash
export RPENT_REAL_ENDPOINT=http://127.0.0.1:9001
curl --noproxy '*' -fsS --max-time 5 "$RPENT_REAL_ENDPOINT/call" \
  -H 'Content-Type: application/json' \
  --data-binary '{"method":"env.list_robots","args":[],"kwargs":{},"session_id":null}'
```

从 `result.robots` 中核对目标本体的 `robot_index`。编号用于当前连接路由，断线重连或服务重启后重新核对编号与实体的对应关系。按实际结果设置编号，再加载真机配置。

```bash
export RPENT_ROBOT_INDEX=1
set -a
source robots/unitree/real_local.env
set +a

bash run_rpent.sh "real-status-$(date -u +%Y%m%dT%H%M%SZ)" \
  '只查询当前 Go2 的在线、心跳和相机状态，报告结果，不执行移动或抓拍。'
```

输出在 `output/real-integration/runs/`。只读查询的 `online` 表示连通且心跳有效。给出相对移动任务前，由操作者确认目标本体、控制器就绪和运动区域。一次只由一个任务进程控制该本体。

## 通过 MCP 调度

在已加载上述配置的终端启动真机 runner。示例使用 `8781`，与本机使用 `8780` 的仿真 runner 分开。模型和 `RPENT_RUNNER_TOKEN` 从现有私有配置加载。

```bash
../.venv/bin/python -m robots.unitree.task_server \
  --host 127.0.0.1 --port 8781 \
  --jobs-dir output/real-integration/jobs \
  --robot-id go2-real
```

跨服务器访问时按[任务 MCP 服务](TASK_MCP.zh-CN.md)配置 RPent 的监听地址、可信 Host 和令牌。DSH 使用真机 runner 的 `/mcp/` 地址，以 `go2-real` 为机器人编号提交任务并查询结果。

前台 runner 用 Ctrl+C 关闭。运行中可用任务停止接口请求取消，继续查询任务终态。

## 真机工具与执行结果

| RPent 工具 | 通信服务方法与结果 |
| --- | --- |
| `list_robots()` | `env.list_robots` 查询当前在线本体 |
| `get_robot_status()` | `healthz` 查询已配置本体的连接、心跳和相机状态 |
| `move_relative(dx_m, dy_m, arrival_turn_deg)` | `env.execute_command` 提交 `move_relative`，按服务端编号等待本体反馈 |
| `capture_image(rgbd=False, after_motion=False)` | `env.capture_image` 提交抓拍并等待照片保存 |
| `get_robot_request(request_id)` | `env.get_request` 查询服务端分配的请求编号 |
| `stop_robot()` | `env.stop` 清空此本体队列并请求停止，检查停稳证据 |

坐标以每次动作开始时的机身为准，前为正 x，左为正 y，合成位移最多 5 米。机器人先转向目标再前进，随后以实际到达朝向为基准追加 `arrival_turn_deg`。`after_motion` 只保证抓拍顺序，RPent 在上一动作得到成功结果后才提交下一动作。

移动须收到真实完成和停车证据。`queued`、`dry_run`、`unknown` 和仅收到停止回执都不能证明到达或物理停稳。查询进度时使用响应内的 `result.request_id`。RPent 为每次提交建立关联值和幂等键，超时或丢失回执时保留待核对状态。取消正在等待的移动时，RPent 请求停车并继续查原运动反馈。

`states.json` 记录工具结果，`real/latest.json` 记录真机请求和停车状态。MCP 任务结果中的 `navigation.real_robot` 来自后者。任务返回 `requires_attention` 时，先核对原请求与现场运动状态，再恢复任务调度。

`RPENT_REAL_ACTION_TIMEOUT_S` 默认每次移动或抓拍等待 180 秒。`RPENT_PLANNER_TIMEOUT_S` 仍控制整个模型任务的预算。

# 通过 MCP 连接 Go2 真机

真机 MCP 绑定通信服务中一个已核对的 `robot_index`，提供在线查询、相对移动、抓拍和停车。通信程序和 RPent 同机时使用 `http://127.0.0.1:9001`。分开部署时填通信程序的局域网根地址。

## 注册服务并查询状态

在通信服务的在线列表核对本体编号，断线重连后重新核对。在 RPent 仓库根目录执行，按现场地址和编号修改示例。

```bash
export RPENT_VENV_DIR="$(cd .. && pwd)/.venv"
export RPENT_MCP_CONFIG="$PWD/mcp.real.local.toml"
export RPENT_RUNS_DIR="$PWD/output/real-integration/runs"
export RPENT_MEMORY_DIR="$PWD/output/real-integration/memory"

"$RPENT_VENV_DIR/bin/rpent" mcp add real \
  --tool-timeout-sec 210 --env-from RPENT_TASK_OUTPUT_DIR \
  -- "$RPENT_VENV_DIR/bin/python" -m robots.unitree.mcp.real.server \
  --endpoint http://127.0.0.1:9001 --robot-index 1

bash run_rpent.sh "go2-status-$(date -u +%Y%m%dT%H%M%SZ)" \
  '查询 Go2 的在线、心跳和相机状态，报告结果。'
```

DSH 通过 [RPent 任务 MCP](TASK_MCP.zh-CN.md) 下发同样的任务。启动 runner 时选择这份 `RPENT_MCP_CONFIG`。同机运行多个 runner 时，为各自设置监听端口、`--robot-id` 和任务目录。

## 调用与验收

| MCP 能力 | 用途 |
| --- | --- |
| `list_robots`、`get_robot_status` | 查询在线编号和已配置本体的连接、心跳、相机状态 |
| `move_relative(dx_m, dy_m, arrival_turn_deg)` | 转向并前往机身坐标下的目标，再相对到达朝向转动 |
| `capture_image(rgbd, after_motion)` | 等待抓拍完成并返回照片路径及元数据 |
| `get_robot_request(request_id)` | 查询通信服务分配的请求和反馈 |
| `stop_robot()` | 清空本体的动作队列，请求停车并检查反馈 |

每次相对移动以动作开始时的朝向为准。正 x 向前，正 y 向左，合成位移最多 5 米，正角向左。移动前由现场操作者确认本体、控制器和场地。动作默认最多等待 180 秒，可用真机 MCP 的 `--action-timeout-s` 调整。

以已完成的运动反馈和停车证据确认执行。`real/latest.json` 记录请求编号和停车状态，DSH 可在任务的 `navigation.real_robot` 中读取。反馈丢失或停车未确认时，先核对现场和原请求。收到中断时，MCP 服务会尝试停止并核对原运动。

按 A* 地图自动导航需要真机实时定位和路径跟踪。具备实测起点和朝向时，可用 [A* 短路线测试](REAL_TEST_PLAN.zh-CN.md)验证一次直线移动。

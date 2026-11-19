# RPent + A* + Go2 真机测试

测试链路为 **RPent → A* `/plan` → Go2 通信程序 `/call` → 机器狗**。在机器狗还没有地图定位接口的情况下，使用现场实测的地图起点和朝向，执行一段短直线。RPent 的 `real_astar_check` 命令负责规划、坐标换算、提交动作和查询结果。首轮测试使用人工验收到达位置。

## 1. 核对 A* 和 Go2 通信地址

在 RPent 服务器的 Bash 终端执行。下例假设 A* 和 Go2 通信程序都在服务器本机。

```bash
cd /home/yanlinjie/robot_project/rpent
export RPENT_NAV_ENDPOINT=http://127.0.0.1:8765
export RPENT_REAL_ENDPOINT=http://127.0.0.1:9001

curl --noproxy '*' -fsS --max-time 5 "$RPENT_NAV_ENDPOINT/health" | jq
curl --noproxy '*' -fsS --max-time 5 "$RPENT_REAL_ENDPOINT/call" \
  -H 'Content-Type: application/json' \
  --data-binary '{"method":"env.list_robots","args":[],"kwargs":{},"session_id":null}' | jq
```

| 通信程序部署位置 | `RPENT_REAL_ENDPOINT` |
| --- | --- |
| 与 RPent 同机 | `http://127.0.0.1:9001` |
| 在另一台局域网服务器 | `http://<通信服务器IP>:9001` |
| 在机器狗上，且服务器能访问它的 `9001/call` | `http://<机器狗IP>:9001` |

`RPENT_REAL_ENDPOINT` 填 HTTP 根地址，RPent 会自动加 `/call`。A* 健康检查应返回 `ok=true`，记录 `map_id` 和 `map_version`，并确认这份地图与测试地点一致。从在线列表核对目标本体的 `robot_index`，以及 `connected=true`、`heartbeat_healthy=true`。重连或重启后重新核对编号。

如 A* 尚未启动，从 `robot_project` 目录选用对应现场的地图包。将下面的地图编号和版本替换为实际值。

```bash
cd /home/yanlinjie/robot_project
ROBOT_MAP='地图ID/地图版本' bash a/scripts/run_robot_astar.sh start
```

## 2. 实测坐标并查看规划

在现场选一段约 **0.30 米**的平直空地。用地图中能与现场对应的标记确定本体起点 `x/y`、初始朝向 `yaw` 和目标 `x/y`。位置用米，朝向用弧度。沿地图正 x 方向时 `yaw=0`，从正 x 转向正 y 为正角。起点和朝向无法核对时，只做规划检查。

回到 RPent 目录，在同一个终端输入实际坐标。`real_astar_check` 默认只规划，终端会打印本次 `plan.json` 的路径。

```bash
cd /home/yanlinjie/robot_project/rpent
read -r -p '输入 A* 的 map_id map_version：' SITE_MAP SITE_VERSION
read -r -p '输入实测起点 x y yaw（米、米、弧度）：' START_X START_Y START_YAW
read -r -p '输入实测目标 x y（米）：' GOAL_X GOAL_Y

astar_args=(
  --nav-endpoint "$RPENT_NAV_ENDPOINT"
  --map-id "$SITE_MAP" --map-version "$SITE_VERSION"
  --start "$START_X" "$START_Y" "$START_YAW"
  --goal "$GOAL_X" "$GOAL_Y"
)

../.venv/bin/python -m robots.unitree.real_astar_check "${astar_args[@]}"
```

核对 `plan.json` 的地图、起终点和 `legs[0].path`。本测试只执行**无告警、两个路径点、长度大于零且不超过 0.5 米**的规划。`check.json` 给出换算后的 `dx_m`、`dy_m` 和 `arrival_turn_deg`。它们使用本次起点的机身坐标，正 x 向前、正 y 向左。Go2 会先朝向目标再前进。

## 3. 执行一次真机动作

现场由一名操作员负责本体，确认控制器就绪、走行范围清空，并准备好手动停止方式。如果真机 runner 已启动，先等它空闲，再暂停这条任务入口，避免同时收到其他移动命令。[Go2 用户手册与遥控器说明](https://www.unitree.com/app/go2/)

在同一终端，把 `1` 改为第 1 步核对的本体编号。追加 `--execute` 会重新向 A* 规划并展示将下发的动作。现场确认本体仍在填入的起点、朝向正确、路线与现场一致时，输入 `MOVE`。

```bash
export RPENT_ROBOT_INDEX=1  # 改为核对过的实际编号
../.venv/bin/python -m robots.unitree.real_astar_check "${astar_args[@]}" --execute
```

程序只提交一条相对移动，并按服务端 `request_id` 查询结果。按 Ctrl+C 可请求中断，仍要核对本体是否停稳。网络或反馈异常时，以现场手动停止和实际状态为准。

## 4. 验收和记录

在 `output/real-integration/astar-checks/` 中找到**执行那次**终端打印的输出目录。每次规划生成独立目录。

| 证据 | 通过条件 |
| --- | --- |
| `plan.json` | 地图编号和版本正确，`legs[0].path` 是预期的短直线 |
| `check.json` | 相对移动参数与实际起点、朝向及 A* 目标一致 |
| `execution.json` | `status=completed`，`result.dry_run=false`，有服务端 `request_id` |
| `real/latest.json` | `completed_moves=1`、`stop_confirmed=true`，无 `stop_error` |
| 现场观察 | Go2 向预期位置移动一次，并在目标附近停稳 |

若规划告警、路径绕行或地图版本不匹配，检查现场坐标和地图后重新规划。若移动请求超时、`submission_uncertain=true` 或停稳未确认，先核对现场，并用已有的 `request_id` 查询原请求结果。确认原动作结果后再决定是否开始下一轮。

本测试的到达位置由现场复核。需要按地图自动跟踪多段路线时，下一步是接入与 A* 地图对齐的实时 `x/y/yaw`。通用真机服务入口见 [Go2 真机使用指南](REAL_USAGE.zh-CN.md)。

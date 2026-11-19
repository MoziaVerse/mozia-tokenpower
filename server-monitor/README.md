# Server Monitor

Python 3.9+ 标准库实现，无第三方依赖。一个配置对应一个 systemd 实例；一个服务故障、等待恢复或飞书超时，不会阻塞其他实例。

## 配置

`vpn.example.json` 保留现有 VPN 检测和重连策略。另一个只检查网站、不重启的配置可以是：

```json
{
  "name": "Website",
  "interval": 30,
  "failures_to_alert": 3,
  "checks": [
    {"type": "http", "url": "https://example.com/health", "expected_status": [200, 204]}
  ],
  "recovery": {"enabled": false},
  "feishu": {"webhook_env": "WEBSITE_FEISHU_WEBHOOK", "secret_env": "WEBSITE_FEISHU_SECRET"}
}
```

配置字段：

- `name`：必填，通知中的服务名称。`host` 默认本机主机名，`timezone` 默认 `Asia/Shanghai`。
- `interval`：每轮检查结束后的等待秒数，默认 10；不包含请求耗时。
- `failures_to_alert`：连续失败阈值，默认 2。任一检查连续多轮不健康即可告警；只有允许重启的检查连续多轮失败才会触发恢复。所有检查通过才发送一次恢复通知，并清零计数和提醒间隔。
- `timeout`：单次检查或 systemctl 命令的超时秒数，默认 8。
- `checks`：至少一项，全部通过才健康。每项可设 `on_failure: "alert"`（仅告警）或 `"recover"`（默认，允许触发已启用的恢复）。例如本机进程异常可以重启，但下游 HTTP 检查配置 `"on_failure": "alert"` 后，单独返回 503 不会重启本机。启用恢复时不会让前面的仅告警项遮住后面允许重启的失败项。
  - `systemd`：`unit` 为本机 systemd 单元名。
  - `http`：支持 HTTP/HTTPS，验证 TLS；`method` 为 `GET`（默认）或 `HEAD`；`expected_status` 默认 `[200]`，也可显式设为 `"any"`，仅判断连通性。不会跟随重定向，也不会读取响应正文。`"any"` 适用于 VPN 隧道，不适用于业务健康判断。
- `recovery`：省略时默认禁用。`enabled` 必须为布尔值，`unit` 是明确授权重启的本机 systemd **服务**。
  - `strategy: "systemd"`（默认）：有待处理作业或服务正在切换状态时等待，不重复重启；状态读不清时不重启。命中 `start-limit-hit` 或服务自己的 `RestartPreventExitStatus`（退出码或信号）时，通知人工处理并暂停到所有检查通过，不执行 `reset-failed`。
  - Halo 的 unit 应声明 `RestartPreventExitStatus=78`，通用策略据此判断，其他服务的 78 没有特殊含义。旧配置的 `strategy: "halo_univpn"` 无须迁移，仍额外保留 78 保护。
  - `cooldown` 默认 90 秒，`backoff_step` 默认 30 秒，`max_cooldown` 默认 390 秒。已提交重启或命令出错后，按 90、120、150 秒递增至上限再试；冷却期间继续检测。只有 systemd 已接受的监控重启请求计入次数，提交并不等于恢复成功。
  - `reminder_interval` 默认 900 秒，独立于重试间隔。首次读状态或请求重启失败立即通知；请求已接受或 systemd 在自行恢复时，先给一个 `cooldown` 的观察期，仍不健康才提醒。之后同一事故最多每 15 分钟提醒一次（可按实例调整）。人工介入和确认恢复立即通知，不受提醒节流影响。首次处置就失败或暂停时，用对应通知代替首条异常通知，不连发两条。
- `feishu`：每个配置可独立选择机器人。`webhook` 可直接填 HTTPS 地址；推荐改用 `webhook_env` 指向环境变量，默认 `FEISHU_WEBHOOK`。签名密钥只能通过 `secret_env` 指定环境变量，默认 `FEISHU_SECRET`，必须配置；`timeout` 默认 10 秒。机器人须开启签名校验。真实 webhook、密钥和配置不提交 Git。

新增监控服务只需一份 JSON 配置，不需要在脚本中添加服务名。新增检查类型才需要实现 `(check, timeout) -> 错误原因或 None`、注册到 `CHECKS` 并补充配置校验。不接受配置中的任意 shell 命令或动态导入代码。

## 安装与迁移

以下命令仅为操作说明，不会由修改代码自动执行。先确认文件不存在，避免覆盖已有配置。配置和环境文件应由 root 持有、权限 0600；其父目录不能对普通用户可写，因为配置可授权重启服务。

在生产 checkout `/opt/mozia/mozia-tokenpower` 中：

```sh
install -m 644 server-monitor/server-monitor@.service /etc/systemd/system/
install -m 600 server-monitor/vpn.example.json /opt/mozia/secrets/server-monitor-vpn.json
# 从旧配置迁移通知凭据；切勿把内容打印到日志或提交到 Git。
install -m 600 /opt/mozia/secrets/vpn-monitor.env /opt/mozia/secrets/server-monitor.env
python3 server-monitor/server_monitor.py --config /opt/mozia/secrets/server-monitor-vpn.json --validate
systemctl daemon-reload
# 避免旧新监控同时对同一 VPN 执行重启：
systemctl disable --now vpn-monitor.service
systemctl enable --now server-monitor@vpn.service
systemctl status server-monitor@vpn.service
journalctl -u server-monitor@vpn.service -n 50 --no-pager
```

迁移失败时先停止 `server-monitor@vpn`，恢复旧脚本路径和旧 unit 后再启动旧服务；不要让两个恢复器同时运行。

新增服务时，写入 `/opt/mozia/secrets/server-monitor-<名称>.json`，然后启用 `server-monitor@<名称>`。全局凭据来自 `server-monitor.env`；可选的 `server-monitor-<名称>.env` 覆盖同名变量。配置修改后重启对应实例。仅告警的实例可通过 systemd override 设置普通用户（须有读取配置和检查目标的权限）；模板使用 root 是为了兼容明确启用的本机重启操作。

```sh
python3 -m unittest discover -s server-monitor -p 'test_*.py'
python3 server-monitor/server_monitor.py --config server-monitor/vpn.example.json --validate
# 环境变量已安全注入时，可发送一条测试通知（会检查，但不会重启目标）：
python3 server-monitor/server_monitor.py --config /path/to/config.json --test-notify
```

`--validate` 不连接网络、不检查真实凭据、不重启服务。`--test-notify` 会发送真实消息。

## 边界

- 事故和通知节流状态仅在内存中；监控进程重启后重新计数，可能重复告警。每次请求重启前都会重新读取服务的 systemd 状态与策略。
- 飞书发送失败写入脱敏日志，目前不持久化、不重投；日志无错误也不代表用户已读。需要可靠投递时再增加有界持久化队列。
- 不监控容器内部状态、TCP 协议、响应正文或登录记录；容器化应用可先暴露 HTTP 健康接口。Docker 重启策略尚未实现，不能用 systemd 策略假代替。
- 本机掉电/断网可能无法发送通知，整机可用性应由另一台机器监控。

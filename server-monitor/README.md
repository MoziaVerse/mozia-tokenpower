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
- `failures_to_alert`：连续失败阈值，默认 2。正常一次即清零；同一事故只发送一次异常通知，恢复后发送一次恢复通知。
- `timeout`：单次检查或 systemctl 命令的超时秒数，默认 8。
- `checks`：至少一项，全部通过才健康；按顺序执行，首次失败即结束本轮。
  - `systemd`：`unit` 为本机 systemd 单元名。
  - `http`：支持 HTTP/HTTPS，验证 TLS；`method` 为 `GET`（默认）或 `HEAD`；`expected_status` 默认 `[200]`，也可显式设为 `"any"`，仅判断连通性。不会跟随重定向，也不会读取响应正文。`"any"` 适用于 VPN 隧道，不适用于业务健康判断。
- `recovery`：省略时默认禁用。`enabled` 必须为布尔值，`unit` 是明确授权重启的目标。
  - `strategy: "systemd"`：通用 systemd 重启，先 reset-failed，再非阻塞 restart。
  - `strategy: "halo_univpn"`：仅适用于 `halo-univpn.service`。每次重启前读取退出状态；状态无法读取时不重启；退出码 78 时发送人工处理通知，锁定自动恢复，直到完整健康检查通过。即使 systemd 随后清除了退出码，也不会在本次进程内绕过此锁定。
  - `cooldown` 默认 90 秒，`backoff_step` 默认 30 秒，`max_cooldown` 默认 390 秒。重试间隔逐次增加至上限，期间继续检查健康状态。禁用恢复时完全不会执行恢复命令。
  - VPN 不允许选择通用 systemd 策略，以免绕过认证失败保护。
- `feishu`：每个配置可独立选择机器人。`webhook` 可直接填 HTTPS 地址；推荐改用 `webhook_env` 指向环境变量，默认 `FEISHU_WEBHOOK`。签名密钥只能通过 `secret_env` 指定环境变量，默认 `FEISHU_SECRET`，必须配置；`timeout` 默认 10 秒。机器人须开启签名校验。真实 webhook、密钥和配置不提交 Git。

检测、恢复、通知和事故状态分离：新增检测器只需实现 `(check, timeout) -> 错误原因或 None` 并注册到 `CHECKS`、补充配置校验；新增恢复策略实现 `(recovery, timeout) -> bool` 并注册到 `RECOVERIES`、补充配置校验。返回 False 表示暂停自动恢复、需要人工处理。无需修改告警状态逻辑；不接受配置中的任意 shell 命令或动态导入代码。

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

- 事故状态仅在内存中；监控进程重启后重新计数，可能重复告警。VPN 退出码 78 仍会在每次恢复操作前重新检查。
- 飞书发送失败写入脱敏日志，目前不持久化、不重投；日志无错误也不代表用户已读。需要可靠投递时再增加有界持久化队列。
- 不监控容器内部状态、TCP 协议、响应正文或登录记录；容器化应用可先暴露 HTTP 健康接口。Docker 重启策略尚未实现，不能用 systemd 策略假代替。
- 本机掉电/断网可能无法发送通知，整机可用性应由另一台机器监控。

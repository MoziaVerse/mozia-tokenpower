#!/usr/bin/python3
"""Watch the Halo UniVPN tunnel, restart it when it drops, and report to Feishu.

Runs from the mozia checkout on token-power-sg (/opt/mozia/mozia-tokenpower):
    # /opt/mozia/secrets/vpn-monitor.env (mode 0600):
    #   FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/...
    #   FEISHU_SECRET=...
    install -m 644 vpn-monitor/vpn-monitor.service /etc/systemd/system/
    python3 vpn-monitor/vpn_monitor.py --self-test
    systemctl daemon-reload && systemctl enable --now vpn-monitor
After a git pull: systemctl restart vpn-monitor
"""
import base64
import hashlib
import hmac
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

SERVICE = "halo-univpn"
PROBE_URL = "http://192.168.92.222/"
HOST_LABEL = "TokenPower 新加坡 2"
# Two misses 10s apart flag a drop before systemd's own 30s RestartSec hides it.
INTERVAL = 10
FAILS_TO_ALERT = 2
RECOVER_TIMEOUT = 90
AUTH_REJECTED = "78"  # connect.py exit code when login is refused or needs a verification code
TZ = ZoneInfo("Asia/Shanghai")


def systemctl(*args):
    return subprocess.run(["systemctl", *args, SERVICE], capture_output=True, text=True).stdout.strip()


def check():
    """Return None when the tunnel is healthy, otherwise why it is not."""
    state = systemctl("is-active")
    if state != "active":
        return f"服务 {SERVICE} 状态为 {state}"
    url = urllib.parse.urlsplit(PROBE_URL)
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=8)
    try:
        conn.request("HEAD", url.path or "/")
        conn.getresponse()
        return None
    except (OSError, http.client.HTTPException) as error:
        return f"探测 {PROBE_URL} 失败：{error or type(error).__name__}"
    finally:
        conn.close()


def sign(timestamp, secret):
    # Feishu custom bot: HMAC-SHA256 keyed by "timestamp\nsecret" over an empty message.
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()


def now():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def notify(title, color, fields):
    timestamp = str(int(time.time()))
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "fields": [
                {"is_short": len(value) <= 24, "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"}}
                for label, value in fields
            ]},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": f"vpn-monitor · {SERVICE} · {PROBE_URL}"}]},
        ],
    }
    body = {"timestamp": timestamp, "sign": sign(timestamp, os.environ["FEISHU_SECRET"]),
            "msg_type": "interactive", "card": card}
    request = urllib.request.Request(os.environ["FEISHU_WEBHOOK"], json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        if result.get("code", 0) != 0:
            print(f"Feishu rejected notification: {result}", flush=True)
    except (OSError, ValueError) as error:
        print(f"Feishu notification failed: {error}", flush=True)


def recover(reason, down_at):
    print(f"VPN down: {reason}", flush=True)
    notify("VPN 连接中断", "red", [
        ("服务器", HOST_LABEL), ("中断时间", datetime.fromtimestamp(down_at, TZ).strftime("%Y-%m-%d %H:%M:%S")),
        ("检测结果", reason), ("处理", "正在自动重连"),
    ])
    attempts = 0
    auth_alerted = False
    while check() is not None:
        if systemctl("is-active") != "active" and systemctl("show", "-p", "ExecMainStatus", "--value") == AUTH_REJECTED:
            # Retrying a refused login can lock the account; wait for a person instead.
            if not auth_alerted:
                notify("VPN 认证被拒绝，需人工处理", "orange", [
                    ("服务器", HOST_LABEL), ("时间", now()),
                    ("说明", "登录被拒绝或需要验证码，已停止自动重连。处理后执行 `systemctl restart halo-univpn`，恢复后会自动通知。"),
                ])
                auth_alerted = True
            time.sleep(INTERVAL)
            continue
        attempts += 1
        print(f"Restarting {SERVICE}, attempt {attempts}", flush=True)
        subprocess.run(["systemctl", "reset-failed", SERVICE], check=False)
        subprocess.run(["systemctl", "restart", SERVICE], check=False)
        # The wait also serves as backoff: 90s, 120s, ... capped at 390s between restarts.
        deadline = time.monotonic() + RECOVER_TIMEOUT + min(30 * (attempts - 1), 300)
        while time.monotonic() < deadline and check() is not None:
            time.sleep(5)
    minutes, seconds = divmod(int(time.time() - down_at), 60)
    print("VPN recovered", flush=True)
    notify("VPN 已恢复连接", "green", [
        ("服务器", HOST_LABEL), ("恢复时间", now()),
        ("中断时长", f"{minutes} 分 {seconds} 秒"), ("自动重连次数", str(attempts)),
    ])


def main():
    if "--self-test" in sys.argv:
        # Reference value computed independently with: printf '' | openssl dgst -sha256 -hmac "$(printf '1599360473\ndemo')" -binary | base64
        assert sign("1599360473", "demo") == "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8="
        print("Signature self-check passed")
        return
    if not os.environ.get("FEISHU_WEBHOOK") or not os.environ.get("FEISHU_SECRET"):
        sys.exit("FEISHU_WEBHOOK and FEISHU_SECRET are required")
    if "--test-notify" in sys.argv:
        notify("VPN 监控测试消息", "blue", [("服务器", HOST_LABEL), ("时间", now()), ("当前检测", check() or "正常")])
        return
    print(f"Watching {SERVICE} via {PROBE_URL}", flush=True)
    fails, first_fail_at = 0, 0.0
    while True:
        reason = check()
        if reason is None:
            fails = 0
        else:
            fails += 1
            first_fail_at = first_fail_at if fails > 1 else time.time()
            if fails >= FAILS_TO_ALERT:
                recover(reason, first_fail_at)
                fails = 0
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()

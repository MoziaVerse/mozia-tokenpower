#!/usr/bin/python3
"""One independently supervised monitor per JSON configuration; see README.md."""
import argparse
import base64
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import socket
import subprocess
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo


def fields(value, allowed):
    if not isinstance(value, dict) or value.keys() - set(allowed.split()):
        raise ValueError("Configuration contains an invalid object or unknown field")


def positive(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("Intervals and timeouts must be finite positive numbers")


def unit_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", value):
        raise ValueError("Invalid systemd unit name")


def parse_url(value, schemes):
    if not isinstance(value, str) or not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid URL")
    url = urllib.parse.urlsplit(value)
    if url.scheme not in schemes or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("Invalid URL scheme, host, credentials or fragment")
    _ = url.port  # Validate port without exposing the URL in error messages.
    return url


def load_config(path):
    with open(path, encoding="utf-8") as source:
        config = json.load(source)
    fields(config, "name host timezone interval failures_to_alert timeout checks recovery feishu")
    config = {"host": socket.gethostname(), "timezone": "Asia/Shanghai", "interval": 10,
              "failures_to_alert": 2, "timeout": 8, **config}
    for key in ("name", "host", "timezone"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"{key} must be a nonempty string")
    ZoneInfo(config["timezone"])
    for key in ("interval", "timeout"):
        positive(config[key])
    if type(config["failures_to_alert"]) is not int or config["failures_to_alert"] < 1:
        raise ValueError("failures_to_alert must be a positive integer")
    if not isinstance(config.get("checks"), list) or not config["checks"]:
        raise ValueError("At least one check is required")
    for check in config["checks"]:
        if not isinstance(check, dict) or not isinstance(check.get("type"), str) or check["type"] not in CHECKS:
            raise ValueError("Unknown check type")
        if check["type"] == "systemd":
            fields(check, "type unit")
            unit_name(check.get("unit"))
        else:
            fields(check, "type url method expected_status")
            parse_url(check.get("url"), ("http", "https"))
            check.setdefault("method", "GET")
            check.setdefault("expected_status", [200])
            if check["method"] not in ("GET", "HEAD"):
                raise ValueError("HTTP method must be GET or HEAD")
            statuses = check["expected_status"]
            if statuses != "any" and (not isinstance(statuses, list) or not statuses or
                    any(type(s) is not int or not 100 <= s <= 599 for s in statuses)):
                raise ValueError("expected_status must be HTTP status codes or 'any'")
    recovery = config.get("recovery", {})
    fields(recovery, "enabled strategy unit cooldown backoff_step max_cooldown")
    recovery = {"enabled": False, "strategy": "systemd", "cooldown": 90,
                "backoff_step": 30, "max_cooldown": 390, **recovery}
    if (type(recovery["enabled"]) is not bool or not isinstance(recovery["strategy"], str)
            or recovery["strategy"] not in RECOVERIES):
        raise ValueError("Invalid recovery enabled flag or strategy")
    for key in ("cooldown", "backoff_step", "max_cooldown"):
        positive(recovery[key])
    if recovery["max_cooldown"] < recovery["cooldown"]:
        raise ValueError("max_cooldown must not be less than cooldown")
    if recovery["enabled"] or "unit" in recovery:
        unit_name(recovery.get("unit"))
    if recovery["enabled"] and recovery["strategy"] == "halo_univpn" and recovery["unit"] not in (
            "halo-univpn", "halo-univpn.service"):
        raise ValueError("halo_univpn recovery only supports halo-univpn.service")
    if recovery["enabled"] and recovery["strategy"] == "systemd" and recovery["unit"] in (
            "halo-univpn", "halo-univpn.service"):
        raise ValueError("Use halo_univpn recovery to preserve authentication rejection protection")
    config["recovery"] = recovery
    feishu = config.get("feishu", {})
    fields(feishu, "webhook webhook_env secret_env timeout")
    feishu = {"webhook_env": "FEISHU_WEBHOOK", "secret_env": "FEISHU_SECRET", "timeout": 10, **feishu}
    for key in ("webhook_env", "secret_env"):
        if not isinstance(feishu[key], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", feishu[key]):
            raise ValueError("Invalid Feishu environment variable name")
    positive(feishu["timeout"])
    if "webhook" in feishu:
        parse_url(feishu["webhook"], ("https",))
    config["feishu"] = feishu
    return config


def systemctl(unit, *args, timeout):
    return subprocess.run(["systemctl", *args, unit], capture_output=True, text=True, timeout=timeout)


def check_systemd(check, timeout):
    result = systemctl(check["unit"], "is-active", timeout=timeout)
    if result.returncode != 0 or result.stdout.strip() != "active":
        return f"systemd 服务 {check['unit']} 未运行"
    return None


def check_http(check, timeout):
    url = urllib.parse.urlsplit(check["url"])
    connection = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    conn = connection(url.hostname, url.port, timeout=timeout)
    try:
        path = urllib.parse.urlunsplit(("", "", url.path or "/", url.query, ""))
        conn.request(check["method"], path)
        status = conn.getresponse().status
        if check["expected_status"] != "any" and status not in check["expected_status"]:
            return f"HTTP 状态码 {status} 不符合预期"
        return None
    finally:
        conn.close()


CHECKS = {"systemd": check_systemd, "http": check_http}


def check_health(config):
    for index, check in enumerate(config["checks"], 1):
        try:
            reason = CHECKS[check["type"]](check, config["timeout"])
        except (OSError, subprocess.SubprocessError, http.client.HTTPException) as error:
            # Do not leak URLs, credentials, command output or response bodies.
            reason = f"{check['type']} 检测失败 ({type(error).__name__})"
        if reason:
            return f"检查 {index}: {reason}"
    return None


def restart_systemd(recovery, timeout):
    unit = recovery["unit"]
    for args in (("reset-failed",), ("restart", "--no-block")):
        result = systemctl(unit, *args, timeout=timeout)
        if result.returncode:
            raise RuntimeError("systemctl recovery command failed")
    return True


def restart_halo_univpn(recovery, timeout):
    # Fail closed if status cannot be read. Exit 78 requires human intervention.
    result = systemctl(recovery["unit"], "show", "-p", "ExecMainStatus", "--value", timeout=timeout)
    if result.returncode or not result.stdout.strip().isdigit():
        raise RuntimeError("Cannot read VPN exit status")
    if result.stdout.strip() == "78":
        return False
    return restart_systemd(recovery, timeout)


RECOVERIES = {"systemd": restart_systemd, "halo_univpn": restart_halo_univpn}


def sign(timestamp, secret):
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()


class FeishuNotifier:
    def __init__(self, config):
        self.config = config
        settings = config["feishu"]
        self.webhook = settings.get("webhook") or os.environ.get(settings["webhook_env"], "")
        self.secret = os.environ.get(settings["secret_env"], "")
        parse_url(self.webhook, ("https",))
        if not self.secret:
            raise ValueError("Feishu signing secret environment variable is required")

    def __call__(self, title, color, details):
        config = self.config
        timestamp = str(int(time.time()))
        fields = [("服务器", config["host"]), ("服务", config["name"]),
                  ("时间", datetime.now(ZoneInfo(config["timezone"])).strftime("%Y-%m-%d %H:%M:%S")),
                  *details]
        body = {"timestamp": timestamp, "sign": sign(timestamp, self.secret), "msg_type": "interactive",
                "card": {"header": {"template": color, "title": {"tag": "plain_text", "content": title}},
                         "elements": [{"tag": "div", "text": {"tag": "plain_text",
                                       "content": "\n".join(f"{key}: {value}" for key, value in fields)}}]}}
        url = urllib.parse.urlsplit(self.webhook)
        conn = http.client.HTTPSConnection(url.hostname, url.port, timeout=config["feishu"]["timeout"])
        try:
            # No redirects: never forward a webhook/signature to a different host.
            path = urllib.parse.urlunsplit(("", "", url.path or "/", url.query, ""))
            conn.request("POST", path, json.dumps(body).encode(), {"Content-Type": "application/json"})
            response = conn.getresponse()
            result = json.loads(response.read(65536))
            if response.status != 200 or not isinstance(result, dict) or result.get("code") != 0:
                raise ValueError("Feishu rejected notification")
        except (OSError, ValueError, http.client.HTTPException) as error:
            print(f"Notification failed ({type(error).__name__}); check Feishu configuration/network", flush=True)
        finally:
            conn.close()


class Monitor:
    """Incident state is independent of probes, recovery strategies and delivery."""
    def __init__(self, config, notify):
        self.config, self.notify = config, notify
        self.failures = self.attempts = 0
        self.down_at = self.first_failure = None
        self.next_restart = 0
        self.blocked = False

    def tick(self, reason, now):
        if reason is None:
            if self.down_at is not None:
                self.notify("服务已恢复", "green", [("中断时长", f"{int(now - self.down_at)} 秒"),
                            ("恢复尝试次数", str(self.attempts))])
            self.failures = self.attempts = 0
            self.down_at = self.first_failure = None
            self.next_restart = 0
            self.blocked = False
            return
        self.failures += 1
        if self.first_failure is None:
            self.first_failure = now
        if self.failures < self.config["failures_to_alert"]:
            return
        recovery = self.config["recovery"]
        if self.down_at is None:
            self.down_at = self.first_failure
            self.notify("服务异常", "red", [("检测结果", reason),
                        ("处理", "尝试自动恢复" if recovery["enabled"] else "仅告警，不重启")])
        if not recovery["enabled"] or self.blocked or now < self.next_restart:
            return
        self.attempts += 1
        self.next_restart = now + min(recovery["max_cooldown"],
                                      recovery["cooldown"] + recovery["backoff_step"] * (self.attempts - 1))
        try:
            if not RECOVERIES[recovery["strategy"]](recovery, self.config["timeout"]):
                self.blocked = True
                self.notify("自动恢复已暂停", "orange", [("说明", "VPN 登录被拒绝或需要验证码；请人工处理。检测恢复正常前不会再次自动重启。")])
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            print(f"Recovery failed ({type(error).__name__}); retry after cooldown", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate", action="store_true", help="Validate config only; no network or restart")
    mode.add_argument("--test-notify", action="store_true", help="Send one test message; never restart")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.validate:
            print("Configuration valid (notification credentials not checked)")
            return
        notifier = FeishuNotifier(config)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(2, f"Invalid configuration or missing notification credentials ({type(error).__name__})\n")
    if args.test_notify:
        notifier("服务监控测试", "blue", [("当前检测", check_health(config) or "正常")])
        return
    monitor = Monitor(config, notifier)
    print(f"Watching {config['name']}", flush=True)
    # ponytail: state is process-local; persist incidents if restart-spanning history is needed.
    while True:
        reason = check_health(config)
        monitor.tick(reason, time.monotonic())
        time.sleep(config["interval"])


if __name__ == "__main__":
    main()

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
import signal
import socket
import subprocess
import time
import urllib.parse
from datetime import datetime
from typing import NamedTuple
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
            fields(check, "type unit on_failure")
            unit_name(check.get("unit"))
        else:
            fields(check, "type url method expected_status on_failure")
            parse_url(check.get("url"), ("http", "https"))
            check.setdefault("method", "GET")
            check.setdefault("expected_status", [200])
            if check["method"] not in ("GET", "HEAD"):
                raise ValueError("HTTP method must be GET or HEAD")
            statuses = check["expected_status"]
            if statuses != "any" and (not isinstance(statuses, list) or not statuses or
                    any(type(s) is not int or not 100 <= s <= 599 for s in statuses)):
                raise ValueError("expected_status must be HTTP status codes or 'any'")
        if check.setdefault("on_failure", "recover") not in ("alert", "recover"):
            raise ValueError("on_failure must be alert or recover")
    recovery = config.get("recovery", {})
    fields(recovery, "enabled strategy unit cooldown backoff_step max_cooldown reminder_interval")
    recovery = {"enabled": False, "strategy": "systemd", "cooldown": 90,
                "backoff_step": 30, "max_cooldown": 390, "reminder_interval": 900, **recovery}
    if (type(recovery["enabled"]) is not bool or not isinstance(recovery["strategy"], str)
            or recovery["strategy"] not in ("systemd", "halo_univpn")):
        raise ValueError("Invalid recovery enabled flag or strategy")
    for key in ("cooldown", "backoff_step", "max_cooldown", "reminder_interval"):
        positive(recovery[key])
    if recovery["max_cooldown"] < recovery["cooldown"]:
        raise ValueError("max_cooldown must not be less than cooldown")
    if recovery["enabled"] or "unit" in recovery:
        unit_name(recovery.get("unit"))
    if recovery["enabled"] and recovery["strategy"] == "halo_univpn" and recovery["unit"] not in (
            "halo-univpn", "halo-univpn.service"):
        raise ValueError("halo_univpn recovery only supports halo-univpn.service")
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


class Failure(NamedTuple):
    reason: str
    check: dict


def check_health(config):
    alert = None
    for index, check in enumerate(config["checks"], 1):
        try:
            reason = CHECKS[check["type"]](check, config["timeout"])
        except (OSError, subprocess.SubprocessError, http.client.HTTPException) as error:
            # Do not leak URLs, credentials, command output or response bodies.
            reason = f"{check['type']} 检测失败 ({type(error).__name__})"
        if reason:
            failure = Failure(f"检查 {index}: {reason}", check)
            if not config["recovery"]["enabled"] or check["on_failure"] == "recover":
                return failure
            if alert is None:
                alert = failure
    return alert


def restart_systemd(recovery, timeout, failed_check):
    unit = recovery["unit"]
    keys = "LoadState ActiveState SubState Job Result ExecMainCode ExecMainStatus RestartPreventExitStatus".split()
    result = systemctl(unit, "show", "--all", "-p", ",".join(keys), timeout=timeout)
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or not set(keys).issubset(state) or state["LoadState"] != "loaded":
        raise RuntimeError("无法读取目标服务的 systemd 状态，未请求重启")
    if state["Result"] == "start-limit-hit":
        return "blocked", "已触发 systemd 启动限流，请人工检查"
    active = state["ActiveState"]
    if (state["Job"] or state["SubState"].startswith("auto-restart") or
            active in ("activating", "deactivating", "reloading", "refreshing", "maintenance")):
        return "waiting", "systemd 正在处理目标服务，监控未重复请求重启"
    code, status = state["ExecMainCode"], state["ExecMainStatus"]
    if (active not in ("active", "inactive", "failed") or not state["SubState"] or not state["Result"]
            or code not in ("0", "1", "2", "3") or not re.fullmatch(r"[0-9]{1,3}", status)
            or int(status) > 255 or (code == "0" and status != "0")):
        raise RuntimeError("无法识别目标服务的 systemd 状态，未请求重启")
    prevented = state["RestartPreventExitStatus"].split()
    if code == "1" and status in prevented:
        return "blocked", f"服务退出码 {status} 禁止重启，请人工检查"
    for name in prevented:
        if re.fullmatch(r"[0-9]{1,3}", name) and int(name) <= 255:
            continue
        number = getattr(signal, "SIG" + name.removeprefix("SIG"), None)
        if not isinstance(number, signal.Signals):
            raise RuntimeError("无法识别禁止重启的退出策略，未请求重启")
        if code in ("2", "3") and int(status) == number:
            return "blocked", f"服务因信号 {name} 退出且禁止重启，请人工检查"
    if (active == "active" and failed_check["type"] == "systemd" and
            failed_check["unit"].removesuffix(".service") == unit.removesuffix(".service")):
        return "waiting", "目标服务已启动，等待下一轮健康检查"
    result = systemctl(unit, "restart", "--no-block", "--job-mode=fail", timeout=timeout)
    if result.returncode:
        raise RuntimeError("systemd 未接受重启请求")
    return "requested", "systemd 已接受重启请求，等待健康检查确认"


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
        self.reset_incident()

    def reset_incident(self):
        self.failures = self.recoverable_failures = self.attempts = 0
        self.down_at = self.first_failure = None
        self.next_restart = 0
        self.retry_delay = self.config["recovery"]["cooldown"]
        self.verify_at = None
        self.next_reminder = 0
        self.last_action = ""
        self.blocked = False

    def try_recovery(self, check, now):
        recovery = self.config["recovery"]
        try:
            outcome, self.last_action = restart_systemd(recovery, self.config["timeout"], check)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            outcome = "error"
            self.last_action = (str(error) if isinstance(error, RuntimeError)
                                else f"systemctl 操作未确认 ({type(error).__name__})")
            print(f"Recovery failed: {self.last_action}", flush=True)
        if outcome == "blocked":
            self.blocked = True
            return
        if outcome == "requested":
            self.attempts += 1
        if outcome in ("requested", "error"):
            self.next_restart = now + self.retry_delay
            self.retry_delay = min(recovery["max_cooldown"], self.retry_delay + recovery["backoff_step"])
        if outcome == "error":
            self.verify_at = now
        elif self.verify_at is None:
            self.verify_at = now + recovery["cooldown"]

    def tick(self, failure, now):
        if failure is None:
            if self.down_at is not None:
                self.notify("服务已恢复", "green", [("中断时长", f"{int(now - self.down_at)} 秒"),
                            ("已提交重启次数", str(self.attempts))])
            self.reset_incident()
            return
        self.failures += 1
        recoverable = self.config["recovery"]["enabled"] and failure.check["on_failure"] == "recover"
        self.recoverable_failures = self.recoverable_failures + 1 if recoverable else 0
        if self.first_failure is None:
            self.first_failure = now
        threshold = self.config["failures_to_alert"]
        if self.failures < threshold or self.blocked:
            return
        recovery = self.config["recovery"]
        title, color = None, "red"
        if self.down_at is None:
            self.down_at = self.first_failure
            title = "服务异常"
        action = "仅告警，不重启"
        if self.recoverable_failures >= threshold:
            if now >= self.next_restart:
                self.try_recovery(failure.check, now)
            action = self.last_action
        elif recoverable:
            action = "等待可触发重启的检查连续失败"
        if self.blocked:
            title, color = "自动恢复已暂停", "orange"
        elif (self.verify_at is not None and now >= self.verify_at
                and now >= self.next_reminder and (recoverable or self.attempts)):
            title, color = "自动恢复仍未成功", "orange"
            self.next_reminder = now + recovery["reminder_interval"]
        if title:
            self.notify(title, color, [("检测结果", failure.reason), ("处理", action),
                        ("已提交重启次数", str(self.attempts)), ("中断时长", f"{int(now - self.down_at)} 秒")])


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
        failure = check_health(config)
        notifier("服务监控测试", "blue", [("当前检测", failure.reason if failure else "正常")])
        return
    monitor = Monitor(config, notifier)
    print(f"Watching {config['name']}", flush=True)
    # ponytail: state is process-local; persist incidents if restart-spanning history is needed.
    while True:
        failure = check_health(config)
        monitor.tick(failure, time.monotonic())
        time.sleep(config["interval"])


if __name__ == "__main__":
    main()

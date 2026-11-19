import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import server_monitor as monitor


def show_state(**properties):
    state = {"LoadState": "loaded", "ActiveState": "failed", "SubState": "failed", "Job": "",
             "Result": "exit-code", "ExecMainCode": "1", "ExecMainStatus": "1", "RestartPreventExitStatus": "",
             **properties}
    return subprocess.CompletedProcess([], 0, "\n".join(f"{key}={value}" for key, value in state.items()
                                                       if value is not None))


class ServerMonitorTests(unittest.TestCase):
    def setUp(self):
        self.config = monitor.load_config(Path(__file__).with_name("vpn.example.json"))
        self.down = monitor.Failure("down", self.config["checks"][0])

    def test_config_defaults_and_invalid_inputs(self):
        minimal = {"name": "web", "checks": [{"type": "http", "url": "https://example.com/health"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(minimal))
            config = monitor.load_config(path)
            self.assertFalse(config["recovery"]["enabled"])
            self.assertEqual(config["checks"][0]["expected_status"], [200])
            invalid = [
                {"interval": 0}, {"timeout": float("nan")}, {"failures_to_alert": True},
                {"checks": []}, {"recovery": {"enabled": "false"}},
                {"recovery": {"enabled": True, "strategy": "shell", "unit": "foo"}},
                {"recovery": {"enabled": True, "unit": "--help"}},
                {"recovery": {"reminder_interval": 0}},
                {"checks": [{"type": "systemd", "unit": "foo", "on_failure": True}]},
                {"recovery": {"enabled": True, "strategy": "halo_univpn", "unit": "other"}},
                {"feishu": {"webhook": "http://example.com/hook"}},
                {"checks": [{"type": "http", "url": "https://example.com", "expected_status": []}]},
                {"checks": [{"type": "shell"}]}, {"unexpected": True},
            ]
            for change in invalid:
                with self.subTest(change=change):
                    path.write_text(json.dumps({**minimal, **change}))
                    with self.assertRaises(ValueError):
                        monitor.load_config(path)

    def test_alert_only_threshold_deduplication_and_recovery(self):
        self.config["recovery"]["enabled"] = False
        notify = Mock()
        instance = monitor.Monitor(self.config, notify)
        with patch.object(monitor, "systemctl") as command:
            for reason, now in [(self.down, 0), (None, 10), (self.down, 20), (self.down, 30), (self.down, 40), (None, 50), (None, 60)]:
                instance.tick(reason, now)
            command.assert_not_called()
        self.assertEqual([call.args[0] for call in notify.call_args_list], ["服务异常", "服务已恢复"])
        self.assertIn(("中断时长", "30 秒"), notify.call_args_list[-1].args[2])

    def test_recovery_backoff_and_instance_isolation(self):
        notify = Mock()
        broken = monitor.Monitor(self.config, notify)
        other = monitor.Monitor(self.config, Mock())
        with patch.object(monitor, "restart_systemd", return_value=("requested", "已提交")) as restart:
            for now in (0, 10, 20, 99, 100, 219, 220):
                broken.tick(self.down, now)
                other.tick(None, now)
            self.assertEqual(restart.call_count, 3)
            self.assertEqual(broken.next_restart, 370)
            self.assertEqual(other.failures, 0)
            self.assertEqual(other.attempts, 0)
            other.notify.assert_not_called()
            broken.tick(None, 230)
        self.assertEqual([call.args[0] for call in notify.call_args_list], ["服务异常", "自动恢复仍未成功", "服务已恢复"])
        self.assertIn(("已提交重启次数", "3"), notify.call_args.args[2])

    def test_prohibited_exit_and_start_limit_latch_until_healthy(self):
        for state in (show_state(ExecMainStatus="78", RestartPreventExitStatus="1 78 TERM"),
                      show_state(ExecMainCode="2", ExecMainStatus="15", RestartPreventExitStatus="TERM"),
                      show_state(Result="start-limit-hit")):
            notify = Mock()
            instance = monitor.Monitor(self.config, notify)
            with self.subTest(state=state), patch.object(monitor, "systemctl", return_value=state) as command:
                for now in (0, 10, 1000, 4000):
                    instance.tick(self.down, now)
                self.assertEqual([call.args[1] for call in command.call_args_list], ["show"])
                self.assertEqual([call.args[0] for call in notify.call_args_list], ["自动恢复已暂停"])
                instance.tick(None, 4010)
                self.assertEqual(notify.call_args.args[0], "服务已恢复")
                self.assertIn(("已提交重启次数", "0"), notify.call_args.args[2])

    def test_old_vpn_config_keeps_exit_guard_without_unit_policy(self):
        legacy = {"name": "vpn", "checks": [{"type": "systemd", "unit": "halo-univpn"}],
                  "recovery": {"enabled": True, "unit": "halo-univpn.service", "strategy": "halo_univpn"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(legacy))
            config = monitor.load_config(path)
        with patch.object(monitor, "systemctl", return_value=show_state(ExecMainStatus="78")) as command:
            self.assertEqual(monitor.restart_systemd(config["recovery"], 8, config["checks"][0])[0], "blocked")
            self.assertEqual(command.call_count, 1)

    def test_unreadable_status_and_command_failure_notify_without_counting_restarts(self):
        for result in (subprocess.CompletedProcess([], 1, ""), show_state(ExecMainStatus="invalid"),
                       show_state(RestartPreventExitStatus=None), show_state(RestartPreventExitStatus="?"),
                       show_state(LoadState="not-found"),
                       subprocess.TimeoutExpired("systemctl", 8), None):
            notify = Mock()
            instance = monitor.Monitor(self.config, notify)
            sequence = ([show_state(), subprocess.CompletedProcess([], 1, "private-output")] if result is None else [result])
            with self.subTest(result=result), patch.object(monitor, "systemctl", side_effect=sequence * 4) as command, patch("builtins.print"):
                for now in (0, 10, 20, 99, 100, 909, 910, 911):
                    instance.tick(self.down, now)
                self.assertEqual([call.args[0] for call in notify.call_args_list], ["自动恢复仍未成功"] * 2)
                self.assertEqual([call.args[1] for call in command.call_args_list], (["show", "restart"] if result is None else ["show"]) * 3)
                self.assertNotIn("private-output", str(notify.call_args_list))
                self.assertIn(("已提交重启次数", "0"), notify.call_args.args[2])
                instance.tick(None, 920)
                instance.tick(self.down, 930)
                instance.tick(self.down, 940)
                self.assertEqual([call.args[0] for call in notify.call_args_list[-2:]], ["服务已恢复", "自动恢复仍未成功"])

    def test_systemd_pending_jobs_and_self_recovery_are_not_interrupted(self):
        for state in (show_state(ActiveState="activating", SubState="auto-restart"),
                      show_state(Job="123"), show_state(ActiveState="active", SubState="running")):
            notify = Mock()
            instance = monitor.Monitor(self.config, notify)
            with self.subTest(state=state), patch.object(monitor, "systemctl", return_value=state) as command:
                for now in (0, 10, 20, 99, 100, 110):
                    instance.tick(self.down, now)
                instance.tick(None, 120)
                self.assertTrue(all(call.args[1] == "show" for call in command.call_args_list))
                self.assertEqual([call.args[0] for call in notify.call_args_list], ["服务异常", "自动恢复仍未成功", "服务已恢复"])
                self.assertIn(("已提交重启次数", "0"), notify.call_args.args[2])

    def test_restarts_only_authorized_service_without_resetting_systemd(self):
        recovery = {**self.config["recovery"], "unit": "example.service"}
        http_check = self.config["checks"][1]
        for state in (show_state(ExecMainStatus="78"), show_state(ExecMainStatus="15", RestartPreventExitStatus="TERM"),
                      show_state(ActiveState="active", SubState="running", ExecMainCode="0", ExecMainStatus="0")):
            with self.subTest(state=state), patch.object(monitor, "systemctl", side_effect=[state, subprocess.CompletedProcess([], 0, "")]) as command:
                self.assertEqual(monitor.restart_systemd(recovery, 8, http_check)[0], "requested")
                self.assertEqual([call.args[1] for call in command.call_args_list], ["show", "restart"])
                self.assertEqual(command.call_args.args, ("example.service", "restart", "--no-block", "--job-mode=fail"))

    def test_alert_only_probe_does_not_restart_or_mask_a_recoverable_failure(self):
        self.config["checks"][0]["on_failure"] = "alert"
        notify = Mock()
        instance = monitor.Monitor(self.config, notify)
        with patch.dict(monitor.CHECKS, systemd=Mock(return_value="down"), http=Mock(side_effect=[None, None, None, "503", "503"])), \
                patch.object(monitor, "restart_systemd", return_value=("requested", "已提交")) as restart:
            for now in (0, 10, 1000, 1010):
                instance.tick(monitor.check_health(self.config), now)
            restart.assert_not_called()
            self.assertIn(("处理", "仅告警，不重启"), notify.call_args.args[2])
            failure = monitor.check_health(self.config)
            self.assertEqual(failure.reason, "检查 2: 503")
            instance.tick(failure, 1020)
            restart.assert_called_once_with(self.config["recovery"], 8, self.config["checks"][1])

    def test_http_status_query_and_no_redirects_with_real_server(self):
        paths = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302 if self.path == "/redirect" else 503)
                self.send_header("Location", "/health")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            check = {"type": "http", "url": f"http://127.0.0.1:{server.server_port}/health?ready=1",
                     "method": "GET", "expected_status": [200]}
            self.assertIn("503", monitor.check_http(check, 1))
            check["expected_status"] = "any"
            self.assertIsNone(monitor.check_http(check, 1))
            self.assertEqual(paths, ["/health?ready=1", "/health?ready=1"])
            check.update(url=f"http://127.0.0.1:{server.server_port}/redirect", expected_status=[200])
            self.assertIn("302", monitor.check_http(check, 1))
            self.assertEqual(paths[-1], "/redirect")
            self.assertEqual(len(paths), 3)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_probe_timeout_is_unhealthy_and_short_circuits(self):
        with patch.object(monitor, "systemctl", side_effect=subprocess.TimeoutExpired("systemctl", 8)), \
                patch.object(monitor.http.client, "HTTPConnection") as http:
            self.assertIn("TimeoutExpired", monitor.check_health(self.config).reason)
            http.assert_not_called()

    def test_feishu_configuration_signature_and_redacted_errors(self):
        self.assertEqual(monitor.sign("1599360473", "demo"), "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=")
        self.config["feishu"].update(webhook_env="OTHER_WEBHOOK", secret_env="OTHER_SECRET")
        with patch.dict(os.environ, {"OTHER_WEBHOOK": "https://example.com/private-hook", "OTHER_SECRET": "private-secret"}), \
                patch.object(monitor.http.client, "HTTPSConnection") as connection:
            notifier = monitor.FeishuNotifier(self.config)
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"code":0}'
            notifier("test", "blue", [])
            args = connection.return_value.request.call_args.args
            self.assertEqual(args[:2], ("POST", "/private-hook"))
            self.assertIn("sign", json.loads(args[2]))
            connection.return_value.request.side_effect = OSError("private-hook private-secret")
            with patch("builtins.print") as output:
                notifier("test", "blue", [])
                self.assertNotIn("private-", str(output.call_args))


if __name__ == "__main__":
    unittest.main()

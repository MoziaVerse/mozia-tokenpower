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


class ServerMonitorTests(unittest.TestCase):
    def setUp(self):
        self.config = monitor.load_config(Path(__file__).with_name("vpn.example.json"))

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
                {"recovery": {"enabled": True, "unit": "halo-univpn"}},
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
            for reason, now in [("down", 0), (None, 10), ("down", 20), ("down", 30), ("down", 40), (None, 50), (None, 60)]:
                instance.tick(reason, now)
            command.assert_not_called()
        self.assertEqual([call.args[0] for call in notify.call_args_list], ["服务异常", "服务已恢复"])
        self.assertIn(("中断时长", "30 秒"), notify.call_args_list[-1].args[2])

    def test_recovery_backoff_and_instance_isolation(self):
        notify = Mock()
        broken = monitor.Monitor(self.config, notify)
        other = monitor.Monitor(self.config, Mock())
        with patch.dict(monitor.RECOVERIES, halo_univpn=Mock(return_value=True)):
            restart = monitor.RECOVERIES["halo_univpn"]
            for now in (0, 10, 20, 99, 100, 219, 220):
                broken.tick("down", now)
                other.tick(None, now)
            self.assertEqual(restart.call_count, 3)
            self.assertEqual(broken.next_restart, 370)
            self.assertEqual(other.failures, 0)
            self.assertEqual(other.attempts, 0)
            other.notify.assert_not_called()

    def test_vpn_refusal_latches_until_healthy(self):
        notify = Mock()
        instance = monitor.Monitor(self.config, notify)
        with patch.object(monitor, "systemctl", return_value=subprocess.CompletedProcess([], 0, "78\n")) as command:
            for now in (0, 10, 1000):
                instance.tick("down", now)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[1], "show")
            self.assertTrue(instance.blocked)
            self.assertEqual([call.args[0] for call in notify.call_args_list], ["服务异常", "自动恢复已暂停"])
            instance.tick(None, 1010)
            self.assertFalse(instance.blocked)

    def test_vpn_unreadable_status_never_restarts(self):
        for status in (subprocess.CompletedProcess([], 1, ""), subprocess.CompletedProcess([], 0, "invalid")):
            with self.subTest(status=status), patch.object(monitor, "systemctl", return_value=status) as command:
                with self.assertRaises(RuntimeError):
                    monitor.restart_halo_univpn(self.config["recovery"], 8)
                self.assertEqual(command.call_count, 1)

    def test_restart_commands_and_timeout_do_not_crash_monitor(self):
        with patch.object(monitor, "systemctl", side_effect=[
            subprocess.CompletedProcess([], 0, "0"), subprocess.CompletedProcess([], 0, ""),
            subprocess.CompletedProcess([], 0, "")]) as command:
            self.assertTrue(monitor.restart_halo_univpn(self.config["recovery"], 8))
            self.assertEqual(command.call_args.args, ("halo-univpn.service", "restart", "--no-block"))
        instance = monitor.Monitor(self.config, Mock())
        with patch.object(monitor, "systemctl", side_effect=subprocess.TimeoutExpired("systemctl", 8)):
            instance.tick("down", 0)
            instance.tick("down", 10)
        self.assertEqual(instance.next_restart, 100)

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
            self.assertIn("TimeoutExpired", monitor.check_health(self.config))
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

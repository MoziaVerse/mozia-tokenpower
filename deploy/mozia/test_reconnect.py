import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, call, mock_open, patch

import reconnect


class ProfileSelectionTests(unittest.TestCase):
    def test_only_selects_a_unique_exact_saved_name(self):
        menu = "1:New Connection\r\n2:Exit\r\n<Connection Name List>\r\n3:demoXvpn\r\n7:demo.vpn\r\n-----\r\n"
        for name, listing, expected in (("demo.vpn", menu, "7"), ("Exit", menu, None),
                ("demo", menu, None), ("", menu, None), ("demo.vpn", menu.replace("3:demoXvpn", "3:demo.vpn"), None),
                ("demo.vpn", menu.split("-----")[0], None)):
            with self.subTest(name=name, listing=listing):
                result = subprocess.run([sys.executable, Path(__file__).with_name("reconnect.py"), "--check-menu"],
                                        env={**os.environ, "UNIVPN_PROFILE": name}, input=listing, text=True, capture_output=True)
                self.assertEqual(result.returncode, 78 if expected is None else 0)
                self.assertEqual(result.stdout.strip(), expected or "")

    def test_missing_profile_or_rejected_authentication_stops_retries(self):
        menu = "<Connection Name List>\n7:demo\n-----\n"
        for name, sends in (("missing", []), ("demo", ["7", "1", "demo-user", "demo-pass"])):
            client = Mock(after=menu)
            client.expect.side_effect = [0, 1]
            vendor = Mock(spawn=Mock(return_value=client), TIMEOUT=TimeoutError, EOF=EOFError)
            with self.subTest(name=name), patch.dict(sys.modules, pexpect=vendor), \
                    patch.dict(os.environ, UNIVPN_PROFILE=name), patch.object(sys, "argv", ["reconnect.py"]), \
                    patch.object(os, "open", return_value=10), patch.object(os, "fstat", return_value=Mock(st_mode=0o100600, st_uid=0)), \
                    patch.object(os, "fdopen", mock_open(read_data='{"username":"demo-user","password":"demo-pass"}')), \
                    patch.object(reconnect.signal, "signal"), patch("builtins.print") as log:
                self.assertEqual(reconnect.main(), 78)
                self.assertEqual(client.sendline.call_args_list, [call(value) for value in sends])
                self.assertNotIn("demo-pass", str(log.call_args_list))
                client.close.assert_called_once_with(force=True)

import os
import re
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

    def test_login_retries_ambiguous_failures_but_stops_for_explicit_auth_challenges(self):
        menu = "<Connection Name List>\n7:demo\n-----\n"
        for name, output, expected in (("missing", "", 78), ("demo", "Login failed\nWelcome to UniVPN!", 78),
                ("demo", "Authentication failed: connection timed out", 78), ("demo", "Login failed: invalid password", 77),
                ("demo", "Account is locked", 77), ("demo", "Please enter the verification code: ", 77)):
            client = Mock(after=menu)
            vendor = Mock(spawn=Mock(return_value=client), TIMEOUT=TimeoutError, EOF=EOFError)

            def expect(patterns):
                if patterns is reconnect.PROFILE_LIST:
                    return 0
                return next((index for index, pattern in enumerate(patterns) if isinstance(pattern, str)
                             and re.search(pattern, output)), patterns.index(vendor.EOF))

            client.expect.side_effect = expect
            with self.subTest(name=name, output=output), patch.dict(sys.modules, pexpect=vendor), \
                    patch.dict(os.environ, UNIVPN_PROFILE=name), patch.object(sys, "argv", ["reconnect.py"]), \
                    patch.object(os, "open", return_value=10), patch.object(os, "fstat", return_value=Mock(st_mode=0o100600, st_uid=0)), \
                    patch.object(os, "fdopen", mock_open(read_data='{"username":"demo-user","password":"demo-pass"}')), \
                    patch.object(reconnect.signal, "signal"), patch("builtins.print") as log:
                self.assertEqual(reconnect.main(), expected)
                sends = ["7", "1", "demo-user", "demo-pass"] if name == "demo" else []
                self.assertEqual(client.sendline.call_args_list, [call(value) for value in sends])
                self.assertNotIn("demo-pass", str(log.call_args_list))
                client.close.assert_called_once_with(force=True)

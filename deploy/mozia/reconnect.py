#!/usr/bin/python3
"""Keep the configured UniVPN connection attached without logging client output."""
import argparse
import json
import os
import re
import signal
import stat
import sys


PROFILE_LIST = re.compile(r"(?ms)^<Connection Name List>\r*\n(.*?)^-{3,}\r*\n")


def profile_number(menu, name):
    listing = PROFILE_LIST.search(menu)
    matches = re.findall(rf"(?m)^([0-9]+):{re.escape(name)}\r*$", listing[1]) if listing else []
    if len(matches) != 1:
        raise ValueError("Configured UniVPN profile is missing or ambiguous")
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-menu", action="store_true", help="Check a menu on stdin without logging in")
    args = parser.parse_args()
    name = os.environ.get("UNIVPN_PROFILE", "")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", name):
        print("Set UNIVPN_PROFILE to the saved connection name", file=sys.stderr)
        return 78
    if args.check_menu:
        try:
            print(profile_number(sys.stdin.read(), name))
        except ValueError as error:
            print(error, file=sys.stderr)
            return 78
        return 0

    import pexpect

    try:
        fd = os.open("/opt/mozia/secrets/halo-vpn.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError
            credentials = json.load(source)
            for key in ("username", "password"):
                if not isinstance(credentials[key], str) or not credentials[key] or any(
                        char in credentials[key] for char in "\r\n\x00"):
                    raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        print("Cannot load root-owned mode 0600 VPN credentials", file=sys.stderr)
        return 78

    child = pexpect.spawn("/usr/sbin/ip", ["netns", "exec", "halo-vpn", "./UniVPNCS"],
                          cwd="/usr/local/UniVPN/serviceclient", encoding="utf-8", timeout=45, echo=False)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    connected = False
    try:
        child.expect(PROFILE_LIST)
        try:
            number = profile_number(child.after, name)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 78
        child.sendline(number)
        child.expect_exact("1:Connect")
        child.sendline("1")
        child.expect_exact("Please input the login user name")
        child.sendline(credentials["username"])
        child.expect_exact("Please input the login user password")
        child.sendline(credentials["password"])
        result = child.expect(["Successful login", r"(?i)(login fail|auth.*fail|incorrect|invalid password|locked|verification code)"])
        if result != 0:
            print("VPN authentication rejected or requires interactive verification; automatic retry stopped", flush=True)
            return 78
        child.expect_exact("Succeeded in enabling network extension")
        connected = True
        print("Halo UniVPN connected", flush=True)
        child.expect([pexpect.EOF, "Welcome to UniVPN!", r"(?i)connection.*(lost|broken)"], timeout=None)
        print("VPN connection ended; systemd will reconnect", flush=True)
    except (pexpect.TIMEOUT, pexpect.EOF):
        print("VPN connection failed or timed out; credentials and client output omitted", flush=True)
    finally:
        if child.isalive():
            try:
                if connected:
                    child.sendline("q")
                    child.expect_exact("Welcome to UniVPN!", timeout=8)
                    child.sendline("2")
                    child.expect(pexpect.EOF, timeout=3)
            except (pexpect.TIMEOUT, pexpect.EOF):
                pass
            finally:
                child.close(force=True)
    return 1


def stop(_signum, _frame):
    raise SystemExit(0)


if __name__ == "__main__":
    sys.exit(main())

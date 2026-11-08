#!/usr/bin/env python3
"""Notify ntfy about physical-interface LAN IPv4 addresses, once per boot/change."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

CONFIG = Path("/etc/ubuntu-customizer/ntfy.json")
STATE = Path("/run/ubuntu-ip-notify/sent.json")
PRIVATE = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) != {"url", "topic", "token"}:
        raise ValueError("ntfy requires URL, topic and token")
    if any(not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127
                                                       for c in value) for value in settings.values()):
        raise ValueError("ntfy settings must be nonempty single-line values without whitespace")
    try:
        url = urllib.parse.urlsplit(settings["url"])
        valid = (url.scheme in ("http", "https") and url.hostname and url.port != 0
                 and not url.username and not url.password and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("NTFY_URL must be an HTTP(S) server URL without credentials, query or fragment")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", settings["topic"]):
        raise ValueError("NTFY_TOPIC must use 1-64 letters, digits, underscores or hyphens")
    if not settings["token"].isascii():
        raise ValueError("NTFY_TOKEN must contain ASCII characters")
    return {**settings, "url": settings["url"].rstrip("/")}


def load_config(path=CONFIG):
    if not path.exists():
        return None
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("ntfy configuration must be root-owned and inaccessible to group/others")
    return validate_settings(json.loads(path.read_text()))


def addresses(records, sys_net=Path("/sys/class/net")):
    found = set()
    for interface in records:
        name = interface["ifname"].split("@", 1)[0]
        # Physical NICs (including common VM NICs) have a device link; bridges,
        # veth, loopback and TUN devices do not. No interface-name assumptions.
        if not (sys_net / name / "device").exists():
            continue
        for entry in interface.get("addr_info", []):
            if entry.get("family") != "inet" or entry.get("scope") != "global":
                continue
            address = ipaddress.IPv4Address(entry["local"])
            if any(address in network for network in PRIVATE):
                found.add((name, str(address)))
    return sorted(found)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Bearer token to another endpoint.
        return None


def publish(settings, snapshot, changed):
    lines = ["主机：" + snapshot["hostname"]]
    for name, address in snapshot["addresses"]:
        lines.extend((f"{name}：{address}", f"SSH：ssh ubuntu@{address}"))
    payload = {"topic": settings["topic"], "title": ("服务器 IP 变化：" if changed else "服务器上线：") + snapshot["hostname"],
               "message": "\n".join(lines), "tags": ["computer"]}
    request = urllib.request.Request(settings["url"] + "/", data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + settings["token"],
                                              "Content-Type": "application/json"}, method="POST")
    # Notifications must not depend on v2rayA nodes or build/shell proxy values.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=15) as response:
        result = json.loads(response.read(1024 * 1024))
    if not isinstance(result, dict) or result.get("event") != "message" or not result.get("id") or result.get("topic") != settings["topic"]:
        raise ValueError("ntfy did not acknowledge the published message")


def notify(settings, state=STATE):
    result = subprocess.run(["ip", "-j", "-4", "address", "show", "up", "scope", "global"],
                            capture_output=True, text=True, check=True, timeout=5)
    current = addresses(json.loads(result.stdout))
    if not current:
        return  # DHCP may not have finished; the timer will retry.
    snapshot = {"hostname": socket.gethostname(), "addresses": [list(item) for item in current]}
    try:
        previous = json.loads(state.read_text())
    except (FileNotFoundError, ValueError):
        previous = None
    if previous == snapshot:
        return
    publish(settings, snapshot, changed=previous is not None)
    # Record success only after acknowledgement. /run is cleared on each boot.
    temporary = state.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(snapshot, stream)
    temporary.chmod(0o600)
    temporary.replace(state)
    print("ntfy LAN address notification delivered")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-config", action="store_true", help="Validate local configuration only; never send a message")
    args = parser.parse_args()
    try:
        settings = load_config()
        if args.check_config:
            print("ntfy configuration valid" if settings else "ntfy notifications disabled (no configuration)")
        elif settings:
            notify(settings)
        return 0
    except urllib.error.HTTPError as error:
        print(f"ntfy publish failed: HTTP {error.code}; will retry on the next timer run", file=sys.stderr)
    except Exception as error:
        # Exception text/URLs/responses may contain credentials. Log only type.
        print("ntfy notification failed: " + type(error).__name__ + "; check configuration/network; next timer run will retry", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

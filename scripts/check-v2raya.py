#!/usr/bin/env python3
"""Configure and check the guest's v2rayA before imaging, in a private netns."""
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess as sp
import time
import urllib.error
import urllib.request

PORTS = {"socks5": 20170, "http": 20171, "socks5WithPac": 0, "httpWithPac": 20172, "vmess": 0}
SETTINGS = {
    "transparent": "close", "portSharing": False, "dnsMode": "off", "dnsHijack": "no",
    # Docker enables forwarding; connecting a proxy node must not turn it off.
    "ipforward": True, "pacAutoUpdateMode": "none", "subscriptionAutoUpdateMode": "none",
}
SERVICE_ENV = {
    "V2RAYA_ADDRESS": "0.0.0.0:2017", "V2RAYA_CONFIG": "/etc/v2raya",
    "V2RAYA_V2RAY_BIN": "/usr/bin/v2raya_core", "V2RAYA_V2RAY_ASSETSDIR": "/usr/share/v2raya",
    "V2RAYA_LITE": "true", "V2RAYA_LOG_FILE": "", "V2RAYA_LOG_DISABLE_COLOR": "true",
}
DOCKER_PROXY = {
    "http-proxy": "http://127.0.0.1:20171", "https-proxy": "http://127.0.0.1:20171",
    "no-proxy": "localhost,127.0.0.0/8,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.localhost,.local",
}


def check_namespace():
    original = os.environ.get("UBUNTU_CUSTOMIZER_ORIGINAL_NETNS")
    if not original or os.readlink("/proc/self/ns/net") == original:
        raise RuntimeError("Private network namespace required for v2rayA configuration")
    if os.environ.get("UBUNTU_CUSTOMIZER_GUEST") != "1" or not Path("/tmp/customizer/build-root").is_file():
        raise RuntimeError("Build guest required for v2rayA configuration")


def check_presets(root):
    env = dict(line.split("=", 1) for line in (root / "etc/default/v2raya").read_text().splitlines())
    if env != SERVICE_ENV:
        raise RuntimeError("Unexpected v2rayA service environment")
    docker = json.loads((root / "etc/docker/daemon.json").read_text())
    if docker.get("proxies") != DOCKER_PROXY or "registry-mirrors" in docker:
        raise RuntimeError("Unexpected Docker proxy configuration")
    for name in ("geoip.dat", "geosite.dat"):
        if not (root / "usr/share/v2raya" / name).is_file() or (root / "usr/share/v2raya" / name).stat().st_size == 0:
            raise RuntimeError("Bundled v2rayA rule data missing: " + name)
    release = json.loads((root / "var/lib/ubuntu-customizer/v2raya-release.json").read_text())
    installed = sp.check_output(["dpkg-query", "-W", "-f=${Version}", "v2raya"], text=True).strip()
    if installed != release["version"]:
        raise RuntimeError("Installed v2rayA version differs from verified release")


class API:
    def __init__(self):
        # The build's HTTP_PROXY must never route local API calls off-machine.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, data=None, *, method=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request("http://127.0.0.1:2017/api/" + path,
                                         data=json.dumps(data).encode() if data is not None else None,
                                         headers=headers, method=method)
        with self.opener.open(request, timeout=3) as response:
            result = json.load(response)
        if result.get("code") != "SUCCESS":
            raise RuntimeError("v2rayA API check failed: " + path)
        return result["data"]

    def check_web(self):
        with self.opener.open("http://127.0.0.1:2017/", timeout=3) as response:
            if b"<html" not in response.read().lower():
                raise RuntimeError("v2rayA embedded Web UI is missing")


def configure(api):
    if api.request("account").get("hasAnyAccounts") is not False:
        raise RuntimeError("Refusing to configure v2rayA with existing accounts")
    # token_urlsafe takes bytes, not characters: 24 bytes encode to 32 ASCII
    # characters, meeting v2rayA's 6-32 character password limit.
    token = api.request("account", {"username": "build-check", "password": secrets.token_urlsafe(24)})["token"]
    api.request("ports", PORTS, method="PUT", token=token)
    setting = api.request("setting", token=token)["setting"]
    api.request("setting", {**setting, **SETTINGS}, method="PUT", token=token)
    for path, expected in (("ports", PORTS), ("setting", SETTINGS)):
        actual = api.request(path, token=token)
        if path == "setting":
            actual = actual["setting"]
        if any(actual.get(key) != value for key, value in expected.items()):
            raise RuntimeError("v2rayA did not retain preset: " + path)
    if api.request("touch", token=token).get("running") is not False:
        raise RuntimeError("v2rayA core unexpectedly started during configuration")
    api.check_web()


def check_database(path):
    # Inspect only; settings were written through upstream's supported API,
    # and the temporary account is removed through --reset-password.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
        for table in ("accounts", "servers", "subscriptions"):
            if database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != 0:
                raise RuntimeError("v2rayA image contains credentials or nodes: " + table)
        # Current releases store account hashes in system_config; the accounts
        # table alone is insufficient. Also reject legacy unprefixed account keys.
        if database.execute("SELECT COUNT(*) FROM system_config WHERE key LIKE 'accounts:%' OR instr(key, ':')=0").fetchone()[0]:
            raise RuntimeError("v2rayA image contains credentials in system_config")
        running = database.execute("SELECT value FROM system_config WHERE key='system:running'").fetchone()
        if running and json.loads(running[0]) is not False:
            raise RuntimeError("v2rayA image must not start a node automatically")
        for key, expected in (("ports", PORTS), ("setting", SETTINGS)):
            row = database.execute("SELECT value FROM system_config WHERE key=?", ("system:" + key,)).fetchone()
            if row is None or any(json.loads(row[0]).get(k) != v for k, v in expected.items()):
                raise RuntimeError("v2rayA persistent preset missing: " + key)


def main():
    check_namespace()  # Must precede file writes, processes and API requests.
    check_presets(Path("/"))
    if any(Path("/etc/v2raya").iterdir()):
        raise RuntimeError("Refusing to overwrite an existing v2rayA configuration")
    sp.run(["ip", "link", "set", "lo", "up"], check=True)
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
           "HOME": "/root", "LC_ALL": "C.UTF-8", **SERVICE_ENV}
    process = sp.Popen(["/usr/bin/v2raya", "--log-disable-timestamp"], env=env,
                       stdin=sp.DEVNULL, start_new_session=True)
    try:
        api = API()
        deadline = time.monotonic() + 60
        while True:
            if process.poll() is not None:
                raise RuntimeError("v2rayA exited before Web API became available")
            try:
                api.request("version")
                break
            except (urllib.error.URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Timed out waiting for v2rayA Web API") from None
                time.sleep(.2)
        configure(api)
    finally:
        # Kill any children as well, even if a future release exits unexpectedly.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=15)
        except sp.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        sp.run(["/usr/bin/v2raya", "--reset-password"], env=env, check=True, timeout=30)
    check_database(Path("/etc/v2raya/v2raya.db"))
    print("v2rayA Web UI and persistent proxy presets checked; no accounts or nodes retained.")


if __name__ == "__main__":
    main()

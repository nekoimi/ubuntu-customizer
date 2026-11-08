#!/usr/bin/env python3
"""Read-only diagnostics for the installed local v2rayA proxy."""
import argparse
import os
import socket
import subprocess

PROXY = "http://127.0.0.1:20171"


def service(name):
    try:
        result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5)
        active = result.returncode == 0 and result.stdout.strip() == "active"
    except (OSError, subprocess.TimeoutExpired):
        active = False
    print(f"[{'OK' if active else 'FAIL'}] {name} service")
    return active


def port(number):
    try:
        with socket.create_connection(("127.0.0.1", number), timeout=3):
            pass
    except OSError:
        print(f"[FAIL] 127.0.0.1:{number} is not accepting connections")
        return False
    print(f"[OK] 127.0.0.1:{number} accepts connections")
    return True


def request(label, url, accepted):
    env = {key: value for key, value in os.environ.items()
           if key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    try:
        result = subprocess.run([
            "curl", "--disable", "--silent", "--show-error", "--output", "/dev/null",
            "--write-out", "%{http_code}", "--connect-timeout", "5", "--max-time", "15",
            "--noproxy", "", "--proxy", PROXY, url,
        ], capture_output=True, text=True, timeout=20, env=env)
    except (OSError, subprocess.TimeoutExpired):
        print(f"[FAIL] {label}: curl unavailable or timed out")
        return False
    code = result.stdout.strip()
    success = result.returncode == 0 and code in accepted
    print(f"[{'OK' if success else 'FAIL'}] {label}: HTTP {code or 'unknown'}, curl exit {result.returncode}")
    if not success:
        reason = {5: "Proxy name resolution failed", 6: "Destination name resolution failed",
                  7: "Connection failed", 28: "Request timed out", 35: "TLS handshake failed",
                  56: "Proxy tunnel or data transfer failed", 60: "TLS certificate verification failed"}
        if result.returncode in reason:
            print("       " + reason[result.returncode])
        print("       Check the selected node, its connectivity, DNS and the destination's response.")
    return success


def main():
    argparse.ArgumentParser(description=(
        "Check v2rayA/Docker services, local ports and HTTPS via the local proxy. "
        "No sudo, image pulls, service restarts or configuration changes."
    )).parse_args()
    print("Checking local v2rayA and Docker (this does not inspect Web credentials or nodes)")
    results = [service("v2raya"), service("docker"), port(2017)]
    proxy_ready = port(20171)
    results.append(proxy_ready)
    if proxy_ready:
        results.append(request("GitHub HTTPS via proxy", "https://github.com/", {"200"}))
        results.append(request("Docker Registry via proxy", "https://registry-1.docker.io/v2/", {"200", "401"}))
        print("Registry HTTP 401 is expected without registry credentials and proves HTTPS reachability.")
    else:
        print("[SKIP] HTTPS checks: open http://<server-ip>:2017, import/select a node and connect it.")
    print("This checks proxy reachability; it does not prove Docker's daemon proxy config or an image pull succeeds.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

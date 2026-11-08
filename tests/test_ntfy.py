"""Notification configuration, credential boundaries, filtering and retry behavior."""
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ntfy_builder", ROOT / "scripts/build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
import build_env  # noqa: E402
import ntfy_notify as ntfy  # noqa: E402

SETTINGS = {"url": "https://ntfy.example.test", "topic": "servers", "token": "tk_test_only"}


class BuildConfig(unittest.TestCase):
    def test_optional_all_or_none_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "local.env"
            self.assertIsNone(build_env.ntfy_settings({}, path))
            path.write_text("NTFY_URL='https://ntfy.example.test/'\nNTFY_TOPIC='servers'\nNTFY_TOKEN='tk_file'\n")
            self.assertEqual(build_env.ntfy_settings({"NTFY_TOKEN": "tk_env"}, path), {**SETTINGS, "token": "tk_env"})
            with self.assertRaises(ValueError):
                build_env.ntfy_settings({"NTFY_TOKEN": ""}, path)

    def test_invalid_settings_are_rejected_without_printing_values(self):
        for change in ({"token": ""}, {"topic": "../private-topic"}, {"token": "private\nsecret"},
                       {"url": "https://private:secret@host.test"}, {"url": "https://host.test?private=secret"},
                       {"url": "ftp://host.test"}, {"url": "https://host.test:99999"}):
            with self.subTest(change=change), self.assertRaises(ValueError) as error:
                ntfy.validate_settings({**SETTINGS, **change})
            self.assertNotIn("private", str(error.exception))
            self.assertNotIn("secret", str(error.exception))

    def test_only_private_guest_file_is_written_and_disabled_creates_nothing(self):
        build = builder.Build.__new__(builder.Build)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            build.ntfy_config = None
            build.install_ntfy_config(root)
            self.assertEqual(list(root.iterdir()), [])
            build.ntfy_config = SETTINGS
            with patch.object(builder.os, "open", wraps=os.open) as opened:
                build.install_ntfy_config(root)
            self.assertEqual(opened.call_args.args[2], 0o600)
            path = root / "etc/ubuntu-customizer/ntfy.json"
            self.assertEqual(json.loads(path.read_text()), SETTINGS)
            with self.assertRaises(FileExistsError):
                build.install_ntfy_config(root)

    def test_config_permissions_checked_and_validation_never_sends(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ntfy.json"
            path.write_text(json.dumps(SETTINGS))
            for uid, mode, allowed in ((0, 0o100600, True), (0, 0o100644, False), (1000, 0o100600, False)):
                with self.subTest(uid=uid, mode=mode), \
                        patch.object(ntfy.Path, "stat", return_value=SimpleNamespace(st_uid=uid, st_mode=mode)):
                    if allowed:
                        self.assertEqual(ntfy.load_config(path), SETTINGS)
                    else:
                        with self.assertRaises(ValueError):
                            ntfy.load_config(path)
        with patch("sys.argv", ["ntfy-ip-notify", "--check-config"]), \
                patch.object(ntfy, "load_config", return_value=SETTINGS), \
                patch.object(ntfy, "notify") as notify, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(ntfy.main(), 0)
        notify.assert_not_called()
        self.assertNotIn(SETTINGS["token"], output.getvalue())


class AddressChanges(unittest.TestCase):
    def test_only_physical_rfc1918_ipv4_is_included_and_sorted(self):
        with tempfile.TemporaryDirectory() as folder:
            sys_net = Path(folder)
            for name in ("ens33", "wlp2s0"):
                (sys_net / name / "device").mkdir(parents=True)
            def record(name, *addresses):
                return {"ifname": name, "addr_info": [{"family": "inet", "scope": "global", "local": ip} for ip in addresses]}
            records = [record("ens33", "192.168.1.5", "192.168.1.5", "8.8.8.8", "169.254.1.2", "100.64.1.1"),
                       record("wlp2s0", "10.1.1.3"), record("docker0", "172.17.0.1"),
                       record("veth-test", "10.0.0.4"), record("tailscale0", "100.100.1.1"), record("lo", "127.0.0.1")]
            self.assertEqual(ntfy.addresses(records, sys_net), [("ens33", "192.168.1.5"), ("wlp2s0", "10.1.1.3")])

    def test_no_address_waits_failure_retries_success_deduplicates_change_and_boot_send(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(ntfy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "[]", "")), \
                patch.object(ntfy.socket, "gethostname", return_value="ubuntu-test"), \
                patch.object(ntfy, "addresses") as addresses, patch.object(ntfy, "publish") as publish, \
                redirect_stdout(io.StringIO()):
            state = Path(folder) / "sent.json"
            addresses.return_value = []
            ntfy.notify(SETTINGS, state)
            publish.assert_not_called()
            self.assertFalse(state.exists())
            addresses.return_value = [("ens33", "10.1.1.5")]
            publish.side_effect = urllib.error.URLError("unreachable")
            with self.assertRaises(urllib.error.URLError):
                ntfy.notify(SETTINGS, state)
            self.assertFalse(state.exists())
            publish.side_effect = None
            ntfy.notify(SETTINGS, state)
            self.assertFalse(publish.call_args.kwargs["changed"])
            publish.reset_mock()
            ntfy.notify(SETTINGS, state)
            publish.assert_not_called()
            addresses.return_value = [("ens33", "10.1.1.6")]
            ntfy.notify(SETTINGS, state)
            self.assertTrue(publish.call_args.kwargs["changed"])
            state.unlink()  # /run is cleared at boot.
            ntfy.notify(SETTINGS, state)
            self.assertFalse(publish.call_args.kwargs["changed"])

    def test_runtime_errors_do_not_log_tokens_or_urls(self):
        with patch("sys.argv", ["ntfy-ip-notify"]), patch.object(ntfy, "load_config", return_value=SETTINGS), \
                patch.object(ntfy, "notify", side_effect=urllib.error.URLError(SETTINGS["token"])), \
                redirect_stderr(io.StringIO()) as output:
            self.assertEqual(ntfy.main(), 1)
        self.assertNotIn(SETTINGS["token"], output.getvalue())


class LocalPublish(unittest.TestCase):
    def test_bearer_json_acknowledgement_and_redirect_refusal_without_shell_proxy(self):
        requests = []
        mode = ["ok"]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, self.headers.get("Authorization"),
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                if mode[0] == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/unexpected")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"event": "message", "id": "test-id", "topic": "servers"}
                                            if mode[0] == "ok" else {"error": "unexpected"}).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings = {**SETTINGS, "url": f"http://127.0.0.1:{server.server_port}"}
            snapshot = {"hostname": "ubuntu-test", "addresses": [["ens33", "10.1.1.5"]]}
            with patch.dict(os.environ, {"http_proxy": "http://invalid.test:1", "NO_PROXY": ""}):
                ntfy.publish(settings, snapshot, changed=False)
                self.assertEqual(requests[0][1], "Bearer " + SETTINGS["token"])
                self.assertIn("ssh ubuntu@10.1.1.5", requests[0][2]["message"])
                mode[0] = "bad-ack"
                with self.assertRaises(ValueError):
                    ntfy.publish(settings, snapshot, changed=False)
                mode[0] = "redirect"
                with self.assertRaises(urllib.error.HTTPError):
                    ntfy.publish(settings, snapshot, changed=True)
            self.assertEqual([item[0] for item in requests], ["/", "/", "/"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

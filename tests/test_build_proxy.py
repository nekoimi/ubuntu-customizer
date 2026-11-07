"""Build proxy configuration, real urllib routing and credential boundaries."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import os
from pathlib import Path
import shlex
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("proxy_builder", ROOT / "scripts/build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
import build_env  # noqa: E402; build.py makes its sibling importable.


class ProxyConfiguration(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "local.env"

    def test_password_and_proxy_file_accept_literal_values_and_empty_fields(self):
        self.path.write_text("UBUNTU_INITIAL_PASSWORD='literal $(command) password'\n"
                             "HTTP_PROXY='http://user:p%40ss@proxy.test:20171'\nHTTPS_PROXY=''\n"
                             "NO_PROXY='localhost,.aliyun.com'\n")
        self.assertEqual(build_env.password_input({}, self.path), "literal $(command) password")
        result = build_env.proxy_environment({}, self.path)
        self.assertEqual(result["http_proxy"], "http://user:p%40ss@proxy.test:20171")
        self.assertEqual(result["https_proxy"], "")
        self.assertEqual(result["NO_PROXY"], "localhost,.aliyun.com")

    def test_environment_overrides_file_across_case_and_empty_disables(self):
        self.path.write_text("http_proxy='http://file.test:80'\nHTTPS_PROXY='http://file.test:80'\n")
        result = build_env.proxy_environment({"HTTP_PROXY": "http://env.test:80", "https_proxy": ""}, self.path)
        self.assertEqual(result["http_proxy"], "http://env.test:80")
        self.assertEqual(result["HTTPS_PROXY"], "")
        result = build_env.proxy_environment({"http_proxy": "", "HTTP_PROXY": "http://env.test:80"}, self.path)
        self.assertEqual(result["HTTP_PROXY"], "")

    def test_invalid_proxy_is_rejected_without_echoing_credentials(self):
        for value in ("socks5://user:private@proxy:20170", "http://proxy:bad", "http://proxy:0",
                      "http://proxy:99999", "http://", "http://proxy/path", "http://proxy\nother"):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                build_env.proxy_environment({"HTTP_PROXY": value}, self.path)
            self.assertNotIn("private", str(raised.exception))

    def test_unknown_keys_and_shell_commands_are_rejected(self):
        for text in ("HTTP_PROXY=x y", "export HTTP_PROXY=x", "curl https://example.com", "UNEXPECTED=x"):
            self.path.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                build_env.proxy_environment({}, self.path)

    def test_guest_proxy_is_quoted_on_stdin_and_not_in_command_or_files(self):
        build = builder.Build.__new__(builder.Build)
        build.proxy_env = {"http_proxy": "http://user:$(literal)@proxy.test:20171", "no_proxy": ""}
        build.run = Mock()
        build.guest(Path("guest"), "apt-get update\n")
        argv = build.run.call_args.args[0]
        script = build.run.call_args.kwargs["input"].decode()
        self.assertNotIn(build.proxy_env["http_proxy"], str(argv))
        self.assertIn("export http_proxy=" + shlex.quote(build.proxy_env["http_proxy"]), script)
        self.assertTrue(script.endswith("apt-get update\n"))
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])

    def test_setup_launcher_forwards_proxy_without_password_or_secret_arguments(self):
        with patch.dict(os.environ, {"UBUNTU_INITIAL_PASSWORD": "private-password"}, clear=True), \
                patch.object(build_env.sys, "argv", ["build_env.py", "--exec", "apt-get", "update"]), \
                patch.object(build_env, "configure_proxy", side_effect=lambda: os.environ.update(http_proxy="http://proxy:20171")), \
                patch.object(build_env.os, "execvp") as execute:
            build_env.main()
            self.assertNotIn("UBUNTU_INITIAL_PASSWORD", os.environ)
            self.assertEqual(os.environ["http_proxy"], "http://proxy:20171")
            execute.assert_called_once_with("apt-get", ["apt-get", "update"])

    def test_released_log_redacts_proxy_and_encoded_credentials(self):
        build = builder.Build.__new__(builder.Build)
        build.owns_release = True
        build.id = "proxy-test"
        build.checks = {}
        build.logs = ["safe phase"]
        build.secret = "initial-password"
        build.password_hash = "$6$hash"
        build.proxy_env = {"https_proxy": "http://proxy-user:p%40ssword@proxy:20171"}
        build.work = Path(self.folder.name) / "work"
        build.release = Path(self.folder.name) / "release"
        build.stage = Path(self.folder.name) / "reports"
        build.work.mkdir()
        build.release.mkdir()
        raw = "http://proxy-user:p%40ssword@proxy:20171 proxy-user p@ssword p%40ssword initial-password $6$hash"
        (build.work / "tools.log").write_text(raw)
        with patch.dict(os.environ, {}, clear=True):
            build.report("failed")
        log = (build.release / "logs/tools.log").read_text()
        self.assertEqual(log, " ".join(["<redacted>"] * 6))
        self.assertNotIn("proxy", (build.stage / "build.log").read_text())


class ProxyDownload(unittest.TestCase):
    def test_download_redirect_uses_proxy_and_no_proxy_bypasses_it(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path == "http://github.invalid/start":
                    self.send_response(302)
                    self.send_header("Location", "http://assets.invalid/file")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"test-payload")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        previous_opener = urllib.request._opener
        try:
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
                path = Path(folder) / "local.env"
                path.write_text("HTTP_PROXY='http://127.0.0.1:" + str(server.server_port) + "'\nNO_PROXY='127.0.0.1'\n")
                build_env.configure_proxy(path=path)
                target = Path(folder) / "download"
                expected = hashlib.sha256(b"test-payload").hexdigest()
                builder.download("http://github.invalid/start", target, expected)
                builder.download("http://127.0.0.1:" + str(server.server_port) + "/direct", target, expected)
                self.assertEqual(target.read_bytes(), b"test-payload")
                self.assertEqual(requests, ["http://github.invalid/start", "http://assets.invalid/file", "/direct"])
        finally:
            urllib.request.install_opener(previous_opener)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

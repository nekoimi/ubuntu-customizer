"""Exercise terminal proxy scope and diagnostics without network access."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name != "nt" else shutil.which("bash", path=r"C:\Program Files\Git\bin")
spec = importlib.util.spec_from_file_location("proxy_check", ROOT / "scripts/proxy-check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


@unittest.skipUnless(BASH, "Bash is required for terminal proxy checks")
class ShellProxy(unittest.TestCase):
    def run_shell(self, commands):
        env = {key: value for key, value in os.environ.items()
               if key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
        return subprocess.run([BASH, "--noprofile", "--norc", "-se"],
                              input="set -u\n" + (ROOT / "scripts/proxy-shell.bash").read_text() + "\n" + commands,
                              env=env, text=True, capture_output=True, timeout=10)

    def test_loading_is_inactive_and_switches_export_to_child_and_clear_all_variants(self):
        result = self.run_shell(r'''
[[ ! -v http_proxy && ! -v HTTPS_PROXY && ! -v ALL_PROXY ]]
proxy-on
bash --noprofile --norc -c '[[ $http_proxy == http://127.0.0.1:20171 && $HTTPS_PROXY == "$http_proxy" && $ALL_PROXY == "$http_proxy" && $NO_PROXY == "$no_proxy" ]]'
proxy-off
bash --noprofile --norc -c 'for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY; do [[ ! -v $v ]] || exit 1; done'
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_status_hides_custom_proxy_credentials_and_arguments_do_not_change_environment(self):
        result = self.run_shell(r'''
export http_proxy=http://private-user:private-password@example.invalid:3128
proxy-status
if proxy-on unexpected; then exit 1; fi
[[ $http_proxy == http://private-user:private-password@example.invalid:3128 ]]
if proxy-off unexpected; then exit 1; fi
[[ -v http_proxy ]]
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("custom value (hidden)", result.stdout)
        self.assertNotIn("private-password", result.stdout + result.stderr)


class ProxyDiagnostics(unittest.TestCase):
    def test_registry_401_is_success_and_explicit_proxy_ignores_environment_and_curlrc(self):
        with patch.dict(os.environ, {"http_proxy": "http://other.invalid", "ALL_PROXY": "socks5://other.invalid",
                                     "NO_PROXY": "*"}), \
                patch.object(check.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "401", "")) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(check.request("registry", "https://registry-1.docker.io/v2/", {"200", "401"}))
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["curl", "--disable"])
        self.assertEqual(args[args.index("--proxy") + 1], "http://127.0.0.1:20171")
        self.assertEqual(args[args.index("--noproxy") + 1], "")
        self.assertNotIn("--insecure", args)
        self.assertFalse(any(key.lower().endswith("_proxy") for key in run.call_args.kwargs["env"]))
        self.assertLessEqual(run.call_args.kwargs["timeout"], 20)

    def test_tls_errors_timeouts_and_unexpected_http_status_fail(self):
        for outcome in (subprocess.CompletedProcess([], 60, "200", ""),
                        subprocess.CompletedProcess([], 0, "403", ""),
                        subprocess.TimeoutExpired("curl", 20), FileNotFoundError()):
            options = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with self.subTest(outcome=outcome), patch.object(check.subprocess, "run", **options), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(check.request("HTTPS", "https://github.com/", {"200"}))

    def test_missing_proxy_port_skips_external_requests_and_returns_failure(self):
        with patch("sys.argv", ["proxy-check"]), patch.object(check, "service", return_value=True), \
                patch.object(check, "port", side_effect=[True, False]), \
                patch.object(check, "request") as request, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check.main(), 1)
        request.assert_not_called()
        self.assertIn("[SKIP]", output.getvalue())

    def test_healthy_endpoints_return_success(self):
        with patch("sys.argv", ["proxy-check"]), patch.object(check, "service", return_value=True), \
                patch.object(check, "port", return_value=True), \
                patch.object(check, "request", return_value=True) as request, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check.main(), 0)
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()

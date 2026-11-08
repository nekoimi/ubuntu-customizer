"""GitHub failure details and isolated download checks without external requests."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("download_builder", ROOT / "scripts/build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class DownloadDiagnostics(unittest.TestCase):
    def test_http_details_omit_credentials_queries_reasons_and_headers(self):
        for code in (403, 404, 407, 429, 502, 503, 504):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://private-user:private-pass@api.github.com/resource?signature=private-query",
                                               code, "private-reason", {"X-Secret": "private-header"}, io.BytesIO(b"private-body"))
                detail = builder.http_error_summary(error)
                self.assertIn(f"HTTP {code} from api.github.com", detail)
                self.assertNotIn("private", detail)

    def test_rate_limit_requires_explicit_header(self):
        error = urllib.error.HTTPError("https://api.github.com/test", 403, "Forbidden",
                                       {"X-RateLimit-Remaining": "0"}, None)
        self.assertIn("rate limit exhausted", builder.http_error_summary(error))
        error.headers = {}
        self.assertNotIn("rate limit exhausted", builder.http_error_summary(error))

    def test_proxy_summary_reports_configured_routes_and_bypass_without_values(self):
        values = {"http_proxy": "http://private-user:private-pass@private-proxy:80", "https_proxy": "",
                  "no_proxy": "api.github.com"}
        summary = "\n".join(builder.proxy_summary(values))
        self.assertIn("HTTP=configured; HTTPS=unset", summary)
        self.assertIn("api.github.com: direct (NO_PROXY match)", summary)
        self.assertIn("github.com: direct (HTTPS proxy unset)", summary)
        self.assertNotIn("private", summary)
        values["https_proxy"] = values["http_proxy"]
        self.assertIn("github.com: proxy", "\n".join(builder.proxy_summary(values)))

    def test_download_check_uses_real_methods_in_tempdir_without_preflight_or_images(self):
        locations = []
        def fake_uv(build, destination):
            locations.append(build.work)
            self.assertTrue(destination.is_dir())
            (destination / "uv").write_bytes(b"test")
        with patch.object(builder, "configure_proxy", return_value={}), \
                patch.object(builder.Build, "uv", autospec=True, side_effect=fake_uv) as uv, \
                patch.object(builder.Build, "v2raya", autospec=True) as v2raya, \
                patch.object(builder.Build, "preflight") as preflight, \
                patch.object(builder.Build, "ubuntu") as ubuntu, redirect_stdout(io.StringIO()):
            self.assertEqual(builder.check_downloads({"uv_version": "latest"}), 0)
        uv.assert_called_once()
        v2raya.assert_called_once()
        preflight.assert_not_called()
        ubuntu.assert_not_called()
        self.assertFalse(locations[0].exists())

    def test_download_check_failure_reports_http_status_and_cleans_tempdir(self):
        locations = []
        def fail_uv(build, destination):
            locations.append(build.work)
            raise urllib.error.HTTPError("https://github.com/file?token=private", 502, "private", {}, None)
        with patch.object(builder, "configure_proxy", return_value={}), \
                patch.object(builder.Build, "uv", autospec=True, side_effect=fail_uv), \
                patch.object(builder.Build, "v2raya") as v2raya, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as console:
            self.assertEqual(builder.check_downloads({"uv_version": "latest"}), 1)
        self.assertIn("HTTP 502 from github.com", console.getvalue())
        self.assertNotIn("private", console.getvalue())
        self.assertFalse(locations[0].exists())
        v2raya.assert_not_called()

    def test_build_failure_shows_http_details_in_console_and_log_without_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            build = Mock(work=Path(folder), loop=None)
            build.ubuntu.side_effect = urllib.error.HTTPError("https://github.com/file?private=query", 404, "private", {}, None)
            with patch.object(builder.sys, "argv", ["build.py"]), \
                    patch.object(builder, "Build", return_value=build), \
                    patch.object(builder.signal, "signal"), patch.object(builder.os, "umask"), \
                    redirect_stderr(io.StringIO()) as console:
                self.assertEqual(builder.main(), 1)
            self.assertIn("HTTP 404 from github.com", console.getvalue())
            self.assertIn("HTTP 404 from github.com", build.note.call_args.args[0])
            self.assertNotIn("private", console.getvalue())
            build.unmount_to.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()

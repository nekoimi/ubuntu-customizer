"""Exercise APK retries and mirror fallback without a chroot or network."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("alpine_builder", ROOT / "image_builder/pipeline.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class AlpinePackages(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "etc/apk").mkdir(parents=True)
        self.build = builder.Build.__new__(builder.Build)
        self.build.config = {"alpine_mirror": "https://primary.test/alpine"}
        self.build.note = Mock()

    def install(self, failures):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, (self.root / "etc/apk/repositories").read_text()))
            if len(calls) <= failures:
                raise subprocess.CalledProcessError(1, argv)

        self.build.run = Mock(side_effect=run)
        with patch.object(builder.time, "sleep") as sleep:
            result = self.build.install_alpine_packages(self.root, "v3.24")
        self.assertEqual(sleep.call_count, failures)
        for argv, repositories in calls:
            self.assertEqual(argv[:5], ["chroot", self.root, "apk", "add", "--no-cache"])
            self.assertIn("linux-lts", argv)
            self.assertIn("sgdisk", argv)
            self.assertNotIn("--allow-untrusted", argv)
            self.assertEqual(len(repositories.splitlines()), 2)
            self.assertIn("/v3.24/main\n", repositories)
            self.assertIn("/v3.24/community\n", repositories)
        return result, calls

    def test_first_success_does_not_retry_or_switch(self):
        result, calls = self.install(0)
        self.assertEqual(result, self.build.config["alpine_mirror"])
        self.assertEqual(len(calls), 1)

    def test_retry_fetches_new_indexes_on_same_mirror(self):
        result, calls = self.install(1)
        self.assertEqual(result, self.build.config["alpine_mirror"])
        self.assertEqual(calls[0], calls[1])

    def test_primary_failures_switch_both_repositories_to_fallback(self):
        result, calls = self.install(2)
        self.assertEqual(result, builder.ALPINE_FALLBACK_MIRRORS[0])
        self.assertEqual(len(calls), 3)
        self.assertNotIn("primary.test", calls[2][1])
        self.assertIn(result + "/v3.24/main", calls[2][1])

    def test_all_failures_raise_last_error_after_bounded_attempts(self):
        failure = subprocess.CalledProcessError(7, ["chroot", "apk"])
        self.build.run = Mock(side_effect=failure)
        with patch.object(builder.time, "sleep") as sleep:
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                self.build.install_alpine_packages(self.root, "v3.24")
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.build.run.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    def test_disabled_fallback_and_interruptions(self):
        self.build.config["alpine_fallback_mirrors"] = []
        for error, attempts in ((subprocess.CalledProcessError(1, ["apk"]), 2),
                                (KeyboardInterrupt(), 1)):
            with self.subTest(error=type(error).__name__):
                self.build.run = Mock(side_effect=error)
                with patch.object(builder.time, "sleep"), self.assertRaises(type(error)):
                    self.build.install_alpine_packages(self.root, "v3.24")
                self.assertEqual(self.build.run.call_count, attempts)

    def test_mirror_list_validation_and_deduplication(self):
        config = self.build.config
        for invalid in ("https://fallback.test", ["http://fallback.test"], [None], ["https://bad.test/\n"]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                builder.alpine_mirrors({**config, "alpine_fallback_mirrors": invalid})
        self.assertEqual(builder.alpine_mirrors({**config, "alpine_fallback_mirrors":
                                               [config["alpine_mirror"] + "/"]}), [config["alpine_mirror"]])


if __name__ == "__main__":
    unittest.main()

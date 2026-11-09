"""Persistent package caches must survive image cleanup and retain signed APT."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cache_builder", ROOT / "scripts/build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PackageCache(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.config = {"work_dir": str(self.base / "work"), "release_dir": str(self.base / "releases"),
                       "ubuntu_mirror": "https://mirror.test/ubuntu/"}
        self.build = builder.Build.__new__(builder.Build)
        self.build.config = self.config
        self.build.cache_base = builder.package_cache_directory(self.config)
        self.build.note = Mock()

    def test_cache_defaults_to_persistent_sibling_and_reuses_existing_debs(self):
        self.assertEqual(self.build.cache_base, self.base / "cache")
        cache = self.build.package_cache()
        package = cache / "test_1_amd64.deb"
        package.write_bytes(b"package-data")
        self.assertEqual(self.build.package_cache(), cache)
        self.assertEqual(package.read_bytes(), b"package-data")
        self.assertIn("1 packages", self.build.note.call_args.args[0])

    def test_invalid_and_overlapping_paths_refuse_before_creating_cache(self):
        for value in (False, "relative", str(self.base), str(self.base / "work"),
                      str(self.base / "releases/cache"), str(ROOT / "cache")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                builder.package_cache_directory({**self.config, "cache_dir": value})

    def test_null_disables_cache_without_creating_directories(self):
        self.build.cache_base = builder.package_cache_directory({**self.config, "cache_dir": None})
        self.assertIsNone(self.build.package_cache())
        self.assertEqual(list(self.base.iterdir()), [])

    def test_debootstrap_shares_archives_and_keeps_repository_keyring_checks(self):
        self.build.run = Mock()
        root = self.base / "guest"
        for cache in (self.base / "cache/apt/archives", None):
            with self.subTest(cache=cache):
                self.build.bootstrap_ubuntu(root, cache)
                command = self.build.run.call_args.args[0]
                self.assertEqual(command[0], "debootstrap")
                self.assertEqual(any(value.startswith("--cache-dir=") for value in map(str, command)), cache is not None)
                self.assertIn("--keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg", command)
                self.assertNotIn("--no-check-gpg", command)

    def test_cleanup_unmounts_cache_before_cleaning_and_removes_build_only_settings(self):
        events = []
        self.build.unmount_to = Mock(side_effect=lambda count: events.append(("unmount", count)))
        self.build.guest = Mock(side_effect=lambda root, script: events.append(("clean", script)))
        self.build.clean_ubuntu(self.base / "guest", 6)
        self.assertEqual(events[0], ("unmount", 6))
        self.assertEqual(events[1][0], "clean")
        self.assertIn("apt-get clean", events[1][1])
        self.assertIn("rm -f /etc/apt/apt.conf.d/99-customizer-build-downloads", events[1][1])

    def test_unmount_failure_never_cleans_persistent_cache(self):
        self.build.unmount_to = Mock(side_effect=subprocess.CalledProcessError(1, ["umount"]))
        self.build.guest = Mock()
        with self.assertRaises(subprocess.CalledProcessError):
            self.build.clean_ubuntu(self.base / "guest", 6)
        self.build.guest.assert_not_called()


if __name__ == "__main__":
    unittest.main()

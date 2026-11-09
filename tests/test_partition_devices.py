"""Resolve SATA/NVMe partitions without udev, using harmless sysfs fixtures."""
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("partition_devices", ROOT / "installer/partition_devices.py")
devices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devices)
build_spec = importlib.util.spec_from_file_location("partition_builder", ROOT / "scripts/build.py")
builder = importlib.util.module_from_spec(build_spec)
build_spec.loader.exec_module(builder)


class PartitionDevices(unittest.TestCase):
    def exercise(self, disk_name, *, missing=False, wrong_device=False, duplicate=False, outside=False, transient=False,
                 missing_all=False, create_missing=False, no_kernel=False, hostile=False, race=False, bad_race=False,
                 denied=False):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            sys_class = base / "sys"
            parent = sys_class / disk_name
            parent.mkdir(parents=True)
            (parent / "dev").write_text("259:0\n")
            dev_root = base / "dev"
            disk = dev_root / disk_name
            disk_path = str(disk)
            nodes = {disk_path: 0}
            expected = {}
            for number in (() if no_kernel else (1, 2, 3)):
                name = disk_name + ("p" if disk_name[-1].isdigit() else "") + str(number)
                child = parent / name
                child.mkdir()
                (child / "partition").write_text(str(number) + "\n")
                (child / "dev").write_text(f"259:{number + 3}\n")
                expected[number] = str(dev_root / name)
                if not missing_all and (not missing or number != 2):
                    nodes[expected[number]] = 99 if wrong_device and number == 2 else number + 3
            if duplicate:
                child = parent / "duplicate"
                child.mkdir()
                (child / "partition").write_text("2\n")
                (child / "dev").write_text("259:4\n")
                nodes[str(dev_root / child.name)] = 4
            if outside:
                child = base / "foreign"
                child.mkdir()
                (child / "partition").write_text("2\n")
                (child / "dev").write_text("259:5\n")
                # Use a junction-like symlink where supported.
                try:
                    (parent / "foreign").symlink_to(child, target_is_directory=True)
                except OSError:
                    self.skipTest("Directory symlinks unavailable")
            original_stat = Path.stat
            original_read = Path.read_text
            self.created = []

            def mknod(path, mode, number):
                self.created.append((str(path), mode, number))
                if denied:
                    raise PermissionError("node creation denied")
                nodes[str(path)] = 99 if bad_race else number
                if race or bad_race:
                    raise FileExistsError(str(path))

            def read_text(path, *args, **kwargs):
                if transient and path.name == "partition":
                    raise FileNotFoundError("sysfs child recreated")
                return original_read(path, *args, **kwargs)

            def fake_stat(path, *args, **kwargs):
                if path.parent == dev_root:
                    if str(path) not in nodes:
                        raise FileNotFoundError(str(path))
                    return SimpleNamespace(st_mode=stat.S_IFLNK if hostile and str(path) == expected[2] else stat.S_IFBLK,
                                           st_rdev=nodes[str(path)])
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", fake_stat), patch.object(Path, "lstat", fake_stat), \
                    patch.object(Path, "read_text", read_text), \
                    patch.object(devices.os, "mknod", side_effect=mknod, create=True), \
                    patch.object(devices.os, "makedev", side_effect=lambda major, minor: minor, create=True), \
                    patch.object(devices.os, "major", return_value=259, create=True), \
                    patch.object(devices.os, "minor", side_effect=lambda value: value, create=True):
                observations = []
                result = devices.probe(disk_path, sys_class, dev_root, observations=observations, create_missing=create_missing)
                if missing:
                    self.assertIn(f"Missing block device node: {expected[2]}", observations)
                    if not create_missing:
                        self.assertIn("Missing verified partitions: 2", observations)
                if transient:
                    self.assertTrue(any("entry disappeared" in item for item in observations))
            return result, expected

    def test_sata_and_nvme_partition_numbers_come_from_kernel_not_lsblk_or_udev(self):
        for disk in ("sda", "nvme0n1", "mmcblk0"):
            with self.subTest(disk=disk):
                result, expected = self.exercise(disk)
                self.assertEqual(result, (expected[3], expected[2]))

    def test_missing_efi_node_is_not_replaced_by_an_unverified_guessed_path(self):
        result, _ = self.exercise("nvme0n1", missing=True)
        self.assertIsNone(result)
        self.assertEqual(self.created, [])

    def test_kernel_partitions_with_all_dev_nodes_missing_are_recreated_with_actual_minor_numbers(self):
        result, expected = self.exercise("nvme0n1", missing_all=True, create_missing=True)
        self.assertEqual(result, (expected[3], expected[2]))
        self.assertCountEqual(self.created, [(expected[n], stat.S_IFBLK | 0o600, n + 3) for n in (1, 2, 3)])

    def test_no_kernel_partitions_never_creates_guessed_nodes(self):
        result, _ = self.exercise("nvme0n1", no_kernel=True, create_missing=True)
        self.assertIsNone(result)
        self.assertEqual(self.created, [])

    def test_existing_wrong_or_symlink_device_is_not_overwritten(self):
        for options in ({"wrong_device": True}, {"hostile": True}):
            with self.subTest(options=options), self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                self.exercise("nvme0n1", create_missing=True, **options)
            self.assertEqual(self.created, [])

    def test_concurrent_node_creation_is_verified_and_denied_creation_stops(self):
        result, expected = self.exercise("nvme0n1", missing=True, create_missing=True, race=True)
        self.assertEqual(result, (expected[3], expected[2]))
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            self.exercise("nvme0n1", missing=True, create_missing=True, bad_race=True)
        with self.assertRaises(PermissionError):
            self.exercise("nvme0n1", missing=True, create_missing=True, denied=True)

    def test_sysfs_children_recreated_during_partition_reread_are_retried(self):
        result, _ = self.exercise("nvme0n1", transient=True)
        self.assertIsNone(result)

    def test_incorrect_device_identity_or_duplicate_partition_number_refuses(self):
        for option in ({"wrong_device": True}, {"duplicate": True}):
            with self.subTest(option=option), self.assertRaises(RuntimeError):
                self.exercise("nvme0n1", **option)

    def test_partition_outside_target_parent_refuses(self):
        with self.assertRaisesRegex(RuntimeError, "outside target"):
            self.exercise("nvme0n1", outside=True)

    def test_delayed_nodes_are_retried_and_timeout_never_returns_partial_result(self):
        expected = ("/dev/nvme0n1p3", "/dev/nvme0n1p2")
        with patch.object(Path, "stat", return_value=SimpleNamespace(st_rdev=1)), \
                patch.object(devices, "probe", side_effect=[None, expected]), \
                patch.object(devices.time, "sleep") as sleep:
            self.assertEqual(devices.wait_for_partitions("/dev/nvme0n1"), expected)
            sleep.assert_called_once_with(0.2)
        with patch.object(Path, "stat", return_value=SimpleNamespace(st_rdev=1)), \
                patch.object(devices, "probe", return_value=None), \
                self.assertRaisesRegex(RuntimeError, "did not become ready"):
            devices.wait_for_partitions("/dev/nvme0n1", timeout=0)

    def test_target_replacement_stops_before_partition_resolution(self):
        with patch.object(Path, "stat", side_effect=[SimpleNamespace(st_rdev=1), SimpleNamespace(st_rdev=2)]), \
                patch.object(devices, "probe") as probe, self.assertRaisesRegex(RuntimeError, "disk changed"):
            devices.wait_for_partitions("/dev/nvme0n1")
        probe.assert_not_called()

    def test_timeout_reports_which_partition_or_node_is_missing(self):
        def not_ready(disk, *, observations, create_missing=False):
            observations.extend(["Kernel partition: nvme0n1p2, number='2', device='259:2'",
                                 "Missing block device node: /dev/nvme0n1p2"])
            return None
        with patch.object(Path, "stat", return_value=SimpleNamespace(st_rdev=1)), \
                patch.object(devices, "probe", side_effect=not_ready):
            with self.assertRaises(RuntimeError) as caught:
                devices.wait_for_partitions("/dev/nvme0n1", timeout=0)
        self.assertIn("number='2'", str(caught.exception))
        self.assertIn("Missing block device node: /dev/nvme0n1p2", str(caught.exception))


class LoopPartitions(unittest.TestCase):
    def test_efi_node_must_also_be_ready_before_formatting(self):
        calls = []

        def node_stat(path):
            calls.append(path.name)
            if path.name.endswith("p2") and calls.count(path.name) == 1:
                raise FileNotFoundError(str(path))
            return SimpleNamespace(st_mode=stat.S_IFBLK)

        with patch.object(Path, "stat", node_stat), patch.object(builder.time, "sleep") as sleep:
            builder.wait_loop_partitions("/dev/loop0")
        sleep.assert_called_once_with(0.1)
        self.assertEqual(calls, ["loop0p2", "loop0p3", "loop0p2", "loop0p3"])

    def test_absent_or_regular_file_nodes_stop_with_explicit_error(self):
        for result in (FileNotFoundError("missing"), SimpleNamespace(st_mode=stat.S_IFREG)):
            with self.subTest(result=result), patch.object(
                    Path, "stat", side_effect=result if isinstance(result, Exception) else None,
                    return_value=result), self.assertRaisesRegex(RuntimeError, "did not become ready"):
                builder.wait_loop_partitions("/dev/loop0", timeout=0)


@unittest.skipUnless(sys.platform == "linux" and hasattr(os, "mknod"), "Linux mknod required")
class RealNodeCreation(unittest.TestCase):
    def test_kernel_fixture_creates_real_block_nodes_without_opening_or_writing_devices(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            sys_class = base / "sys"
            parent = sys_class / "nvme0n1"
            parent.mkdir(parents=True)
            (parent / "dev").write_text("240:0\n")
            dev_root = base / "dev"
            dev_root.mkdir()
            disk = dev_root / "nvme0n1"
            try:
                os.mknod(disk, stat.S_IFBLK | 0o600, os.makedev(240, 0))
            except PermissionError:
                self.skipTest("CAP_MKNOD required; no devices were opened")
            for number in (1, 2, 3):
                child = parent / f"nvme0n1p{number}"
                child.mkdir()
                (child / "partition").write_text(str(number))
                (child / "dev").write_text(f"240:{number + 3}")
            self.assertIsNone(devices.probe(disk, sys_class, dev_root))
            root, esp = devices.probe(disk, sys_class, dev_root, create_missing=True)
            self.assertEqual(root, str(dev_root / "nvme0n1p3"))
            self.assertEqual(esp, str(dev_root / "nvme0n1p2"))
            for number in (1, 2, 3):
                info = (dev_root / f"nvme0n1p{number}").lstat()
                self.assertTrue(stat.S_ISBLK(info.st_mode))
                self.assertEqual(info.st_rdev, os.makedev(240, number + 3))
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()

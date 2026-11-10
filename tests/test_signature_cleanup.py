"""Limit old-signature erasure to the confirmed disk before writing its image."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("signature_installer", ROOT / "installer/installer.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
DISK = {"path": "/dev/sda", "maj:min": "8:0", "identity": ("confirmed",)}
PART = {"path": "/dev/sda1", "type": "part", "maj:min": "8:1"}


class SignatureCleanup(unittest.TestCase):
    def exercise(self, partitions=None, *, parent="sda", changed=False, wipe_status=0, mismatched=False):
        tree = {"blockdevices": [{**DISK, "type": "disk", "children": [PART] if partitions is None else partitions}]}
        def fstat(fd):
            return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=5 if mismatched else 1 if fd == 9 else 0)
        revalidate = Mock(side_effect=[None, installer.Refusal("busy")] if changed else None)
        run = Mock(side_effect=subprocess.CalledProcessError(wipe_status, ["wipefs"]) if wipe_status else None)
        with patch.object(installer, "revalidate", revalidate), \
                patch.object(installer, "output", return_value=json.dumps(tree)) as probe, \
                patch.object(installer, "physical_roots", return_value={parent}), \
                patch.object(installer.os, "open", return_value=9) as opened, \
                patch.object(installer.os, "close") as closed, patch.object(installer.os, "fstat", side_effect=fstat), \
                patch.object(installer.os, "major", return_value=8, create=True), \
                patch.object(installer.os, "minor", side_effect=lambda number: number, create=True), \
                patch.object(installer.os, "O_CLOEXEC", 0x80000, create=True), \
                patch.object(installer.os, "fsync"), patch.object(installer.os, "lseek"), \
                patch.object(installer.sp, "run", run), patch.object(installer.sys, "stdout", io.StringIO()):
            try:
                installer.wipe_old_signatures(7, DISK, 1024)
                error = None
            except Exception as caught:
                error = caught
        self.assertIn("--tree", probe.call_args.args)
        return run, opened, closed, error

    def test_old_partitions_are_erased_before_whole_disk_using_verified_fds(self):
        run, opened, closed, error = self.exercise()
        self.assertIsNone(error)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["wipefs", "--all", "--force", "--", "/proc/self/fd/9"],
            ["wipefs", "--all", "--force", "--", "/proc/self/fd/7"]])
        self.assertEqual([call.kwargs["pass_fds"] for call in run.call_args_list], [(9,), (7,)])
        opened.assert_called_once_with("/dev/sda1", os.O_RDWR | 0x80000)
        closed.assert_called_once_with(9)

    def test_unpartitioned_disk_still_clears_whole_disk_signatures(self):
        run, opened, _, error = self.exercise([])
        self.assertIsNone(error)
        opened.assert_not_called()
        self.assertEqual(run.call_args.args[0][-1], "/proc/self/fd/7")

    def test_outside_partition_mappings_or_unexpected_nodes_refuse_before_erasure(self):
        for options in ({"parent": "sdb"}, {"partitions": [{**PART, "type": "crypt"}]},
                        {"partitions": [{**PART, "children": [PART]}]},
                        {"partitions": [{**PART, "path": "../../other-device"}]}):
            with self.subTest(options=options):
                run, opened, _, error = self.exercise(**options)
                self.assertIsInstance(error, installer.Refusal)
                run.assert_not_called()
                opened.assert_not_called()

    def test_busy_or_replaced_device_never_erases_and_closes_partition(self):
        for options in ({"changed": True}, {"mismatched": True}):
            with self.subTest(options=options):
                run, _, closed, error = self.exercise(**options)
                self.assertIsInstance(error, installer.Refusal)
                run.assert_not_called()
                closed.assert_called_once_with(9)

    def test_cleanup_failure_stops_before_whole_disk_and_image_writes(self):
        run, _, closed, error = self.exercise(wipe_status=1)
        self.assertIsInstance(error, subprocess.CalledProcessError)
        self.assertEqual(run.call_count, 1)
        closed.assert_called_once_with(9)

    def test_main_only_cleans_after_confirmation_and_before_stream_write(self):
        manifest = {"format": 1, "architecture": "amd64", "sector_size": 512, "root_partition": 3,
                    "esp_partition": 2, "raw_bytes": 1024, "raw_sha256": "0" * 64,
                    "compressed_sha256": "0" * 64, "build_id": "test"}
        for cancel, fail_tail in ((False, False), (True, False), (False, True)):
            with self.subTest(cancel=cancel, fail_tail=fail_tail), tempfile.TemporaryDirectory() as folder:
                media = Path(folder)
                payload = media / "payload"
                payload.mkdir()
                (payload / "manifest.json").write_text(json.dumps(manifest))
                (media / "UBUNTU_CUSTOMIZER_MEDIA").write_text("test\n")
                steps = []
                def confirm(_):
                    steps.append("confirm")
                    if cancel:
                        raise installer.Refusal("cancelled")
                def clear_tail(*args):
                    steps.append("tail")
                    if fail_tail:
                        raise OSError("disk-tail write failed")
                with patch.multiple(installer, MEDIA=media, PAYLOAD=payload, check_live_tools=Mock(), welcome=Mock(),
                                    sha256_file=Mock(return_value="0" * 64), discover=Mock(return_value=[DISK]),
                                    select_disk=Mock(return_value=DISK), confirm=Mock(side_effect=confirm), revalidate=Mock(),
                                    wipe_old_signatures=Mock(side_effect=lambda *args: steps.append("wipe")),
                                    write_image=Mock(side_effect=lambda *args: steps.append("write")),
                                    clear_unwritten_tail=Mock(side_effect=clear_tail),
                                    disk_identity=Mock(return_value=DISK["identity"]), interaction_panel=Mock()), \
                        patch.multiple(installer.os, create=True, O_CLOEXEC=0x80000, geteuid=Mock(return_value=0), open=Mock(return_value=7),
                                       close=Mock(), fstat=Mock(return_value=SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=0)),
                                       major=Mock(return_value=8), minor=Mock(return_value=0)), \
                        patch.object(installer.sp, "run") as run:
                    if cancel:
                        with self.assertRaises(installer.Refusal):
                            installer.main()
                    elif fail_tail:
                        with self.assertRaisesRegex(OSError, "disk-tail write failed"):
                            installer.main()
                    else:
                        installer.main()
                finalized = any(call.args[0][0] == "bash" for call in run.call_args_list)
                self.assertEqual(finalized, not cancel and not fail_tail)
                self.assertEqual(steps, ["confirm"] if cancel else ["confirm", "wipe", "write", "tail"])


class UnwrittenTailCleanup(unittest.TestCase):
    def exercise(self, target, raw_bytes, *, selected_size=None, device_matches=True, flush_error=False,
                 identity_changed=False):
        selected = {**DISK, "size": target.stat().st_size if selected_size is None else selected_size}
        ioctl = Mock(side_effect=OSError("cache invalidation failed") if flush_error else None)
        with target.open("r+b", buffering=0) as stream, \
                patch.object(installer, "revalidate", side_effect=installer.Refusal("target changed") if identity_changed else None), \
                patch.object(installer.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=0)), \
                patch.object(installer.os, "major", return_value=8 if device_matches else 9, create=True), \
                patch.object(installer.os, "minor", return_value=0, create=True), \
                patch.dict(sys.modules, {"fcntl": SimpleNamespace(ioctl=ioctl)}), \
                patch.object(installer.sys, "stdout", io.StringIO()):
            installer.clear_unwritten_tail(stream.fileno(), selected, raw_bytes)
            ioctl.assert_called_once_with(stream.fileno(), 0x1261)

    def test_tail_is_cleared_without_changing_image_or_unrelated_middle_bytes(self):
        for image_bytes in (1024 ** 2, 4 * 1024 ** 2, 6 * 1024 ** 2, 7 * 1024 ** 2, 8 * 1024 ** 2):
            with self.subTest(image_bytes=image_bytes), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "disk.img"
                before = b"x" * (8 * 1024 ** 2)
                target.write_bytes(before)
                self.exercise(target, image_bytes)
                after = target.read_bytes()
                start = max(image_bytes, len(before) - 4 * 1024 ** 2)
                self.assertEqual(after[:start], before[:start])
                self.assertEqual(after[start:], bytes(len(before) - start))
                self.assertEqual(target.stat().st_size, len(before))

    def test_wrong_device_or_changed_size_or_invalid_image_never_writes(self):
        for options in ({"device_matches": False}, {"selected_size": 2048},
                        {"raw_bytes": 0}, {"raw_bytes": 8192}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "disk.img"
                target.write_bytes(b"x" * 4096)
                with self.assertRaises(installer.Refusal):
                    self.exercise(target, **{"raw_bytes": 512, **options})
                self.assertEqual(target.read_bytes(), b"x" * 4096)

    def test_write_failure_or_nonzero_readback_refuses(self):
        for operation, result in (("write", 0), ("read", b"x"), ("read", b"")):
            with self.subTest(operation=operation, result=result), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "disk.img"
                target.write_bytes(b"x" * 4096)
                with patch.object(installer.os, operation, return_value=result), \
                        self.assertRaises((OSError, installer.Refusal)):
                    self.exercise(target, 512)

    def test_short_writes_are_completed(self):
        original_write = os.write
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "disk.img"
            target.write_bytes(b"x" * 4096)
            with patch.object(installer.os, "write", side_effect=lambda descriptor, data: original_write(
                    descriptor, data[:max(1, len(data) // 2)])):
                self.exercise(target, 512)
            self.assertEqual(target.read_bytes(), b"x" * 512 + bytes(4096 - 512))

    def test_failed_cache_invalidation_stops_before_readback(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "disk.img"
            target.write_bytes(b"x" * 4096)
            with patch.object(installer.os, "read") as read, \
                    self.assertRaisesRegex(OSError, "cache invalidation failed"):
                self.exercise(target, 512, flush_error=True)
            read.assert_not_called()

    def test_lost_disk_identity_stops_before_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "disk.img"
            target.write_bytes(b"x" * 4096)
            with self.assertRaisesRegex(installer.Refusal, "target changed"):
                self.exercise(target, 512, identity_changed=True)
            self.assertEqual(target.read_bytes(), b"x" * 4096)


@unittest.skipUnless(sys.platform == "linux" and all(shutil.which(tool) for tool in ("mkfs.ext4", "wipefs", "blkid", "resize2fs")),
                     "Linux filesystem tools required; uses temporary regular files, no root or disks")
class ZfsTailRegression(unittest.TestCase):
    def test_missed_zfs_labels_near_future_partition_end_are_cleared_before_ext4_growth(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "disk.img"
            partition = Path(folder) / "partition.img"
            total = 64 * 1024 ** 2
            root_start = 1024 ** 2
            root_end = total - 33 * 512
            root_size = root_end - root_start
            with target.open("wb") as stream:
                stream.truncate(total)
                for label in (root_size // (256 * 1024) * (256 * 1024) - 256 * 1024,
                              root_size // (256 * 1024) * (256 * 1024) - 512 * 1024):
                    for index in range(8):
                        stream.seek(root_start + label + 128 * 1024 + index * 1024)
                        stream.write(struct.pack("<QQ", 0x00bab10c, 5000))
            with partition.open("wb") as stream:
                stream.truncate(16 * 1024 ** 2)
            subprocess.run(["mkfs.ext4", "-F", str(partition)], check=True, capture_output=True)
            with partition.open("rb") as source, target.open("r+b") as destination:
                destination.seek(root_start)
                shutil.copyfileobj(source, destination)
            original_image = target.read_bytes()[:root_start + partition.stat().st_size]

            def extract_and_probe():
                with target.open("rb") as source, partition.open("wb") as destination:
                    source.seek(root_start)
                    destination.write(source.read(root_size))
                return subprocess.run(["blkid", "-p", "-o", "export", str(partition)], capture_output=True, text=True)

            before = extract_and_probe()
            self.assertEqual(before.returncode, 8, before.stdout + before.stderr)
            signatures = subprocess.check_output(["wipefs", "--no-act", str(partition)], text=True)
            self.assertIn("zfs_member", signatures)
            UnwrittenTailCleanup().exercise(target, len(original_image))
            self.assertEqual(target.read_bytes()[:len(original_image)], original_image)
            after = extract_and_probe()
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertIn("TYPE=ext4", after.stdout)
            self.assertNotIn("zfs_member", subprocess.check_output(["wipefs", "--no-act", str(partition)], text=True))
            subprocess.run(["resize2fs", str(partition)], check=True, capture_output=True)
            grown = subprocess.run(["blkid", "-p", "-o", "export", str(partition)], capture_output=True, text=True)
            self.assertEqual(grown.returncode, 0, grown.stderr)
            self.assertIn("TYPE=ext4", grown.stdout)

    def test_old_zfs_tail_is_removed_through_inherited_fd_before_new_ext4_image(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "test.img"
            image = Path(folder) / "template.img"
            total = 64 * 1024 * 1024
            with target.open("wb") as stream:
                stream.truncate(total)
                # Four ZFS uberblocks in the last label are sufficient for the
                # real libblkid scanner; all are beyond the 16 MiB template.
                for index in range(4):
                    stream.seek(total - 128 * 1024 + index * 1024)
                    stream.write(struct.pack("<QQ", 0x00bab10c, 5000))
            before = subprocess.run(["blkid", "-p", "-o", "export", str(target)], capture_output=True, text=True)
            self.assertEqual(before.returncode, 0, before.stderr)
            self.assertIn("TYPE=zfs_member", before.stdout)
            with target.open("r+b") as stream:
                subprocess.run(["wipefs", "--all", "--force", "--", f"/proc/self/fd/{stream.fileno()}"],
                               pass_fds=(stream.fileno(),), check=True, capture_output=True)
            with image.open("wb") as stream:
                stream.truncate(16 * 1024 * 1024)
            subprocess.run(["mkfs.ext4", "-F", str(image)], check=True, capture_output=True)
            with image.open("rb") as source, target.open("r+b") as destination:
                shutil.copyfileobj(source, destination)
            after = subprocess.run(["blkid", "-p", "-o", "export", str(target)], capture_output=True, text=True)
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertIn("TYPE=ext4", after.stdout)
            self.assertNotIn("zfs_member", after.stdout)


if __name__ == "__main__":
    unittest.main()

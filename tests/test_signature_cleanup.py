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
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as folder:
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
                with patch.multiple(installer, MEDIA=media, PAYLOAD=payload, check_live_tools=Mock(), welcome=Mock(),
                                    sha256_file=Mock(return_value="0" * 64), discover=Mock(return_value=[DISK]),
                                    select_disk=Mock(return_value=DISK), confirm=Mock(side_effect=confirm), revalidate=Mock(),
                                    wipe_old_signatures=Mock(side_effect=lambda *args: steps.append("wipe")),
                                    write_image=Mock(side_effect=lambda *args: steps.append("write")),
                                    disk_identity=Mock(return_value=DISK["identity"]), interaction_panel=Mock()), \
                        patch.multiple(installer.os, create=True, O_CLOEXEC=0x80000, geteuid=Mock(return_value=0), open=Mock(return_value=7),
                                       close=Mock(), fstat=Mock(return_value=SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=0)),
                                       major=Mock(return_value=8), minor=Mock(return_value=0)), \
                        patch.object(installer.sp, "run"):
                    if cancel:
                        with self.assertRaises(installer.Refusal):
                            installer.main()
                    else:
                        installer.main()
                self.assertEqual(steps, ["confirm"] if cancel else ["confirm", "wipe", "write"])


@unittest.skipUnless(sys.platform == "linux" and all(shutil.which(tool) for tool in ("mkfs.ext4", "wipefs", "blkid")),
                     "Linux filesystem tools required; uses temporary regular files, no root or disks")
class ZfsTailRegression(unittest.TestCase):
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

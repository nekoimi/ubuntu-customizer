import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


installer = module("installer", "installer/installer.py")
builder = module("builder", "scripts/build.py")


def disk(path, size=64 * 1024 ** 3, **extra):
    return {"path": path, "type": "disk", "size": size, "log-sec": 512, "ro": False,
            "rm": False, "tran": "sata", "serial": path, "maj:min": "8:0", **extra}


class SelectionSafety(unittest.TestCase):
    def candidates(self, nodes, excluded=None, occupied=None):
        return installer.eligible_disks(nodes, excluded or set(), occupied or set(), 24 * 1024 ** 3,
                                        lambda node: (node["path"],))

    def test_largest_disk_does_not_override_media_or_occupied_exclusions(self):
        nodes = [disk("/dev/sda", 512 * 1024 ** 3), disk("/dev/sdb", 1024 * 1024 ** 3), disk("/dev/nvme0n1")]
        result = self.candidates(nodes, {"sdb"}, {"sda"})
        self.assertEqual([d["path"] for d in result], ["/dev/nvme0n1"])

    def test_unsupported_disks_and_mounted_descendants_are_never_candidates(self):
        nodes = [disk("/dev/sda", ro=True), disk("/dev/sdb", rm=True), disk("/dev/sdc", tran="usb"),
                 disk("/dev/sdd", **{"log-sec": 4096}), disk("/dev/sde", 1024),
                 disk("/dev/sdf", children=[{"mountpoints": ["/data"]}]),
                 disk("/dev/loop0", type="loop"), disk("/dev/dm-0", type="lvm"), disk("/dev/sdg", ro=None)]
        self.assertEqual(self.candidates(nodes), [])

    def test_equal_size_choice_is_deterministic_and_largest_is_first(self):
        nodes = [disk("/dev/sdb"), disk("/dev/sda"), disk("/dev/sdc", 128 * 1024 ** 3)]
        self.assertEqual([d["path"] for d in self.candidates(nodes)], ["/dev/sdc", "/dev/sda", "/dev/sdb"])
        self.assertEqual(self.candidates(nodes), self.candidates(list(reversed(nodes))))

    def test_missing_identity_is_excluded(self):
        with patch.object(installer, "disk_identity", side_effect=installer.Refusal("missing")):
            result = installer.eligible_disks([disk("/dev/sda")], set(), set(), 1024, installer.disk_identity)
        self.assertEqual(result, [])

    def test_unknown_media_aborts_instead_of_selecting_largest(self):
        with patch.object(installer, "media_disks", side_effect=installer.Refusal("unknown media")), patch.object(installer, "output") as command:
            with self.assertRaises(installer.Refusal):
                installer.discover(1024)
            command.assert_not_called()

    def test_only_exact_explicit_confirmation_is_accepted(self):
        for answer in ("", "n", "N", "yes", "ERASE", "y ", " y", "1"):
            with self.subTest(answer=answer), self.assertRaises(installer.Refusal):
                installer.confirm(disk("/dev/sda"), lambda _: answer)
        for answer in ("y", "Y"):
            installer.confirm(disk("/dev/sda"), lambda _: answer)

    def test_revalidation_rejects_replaced_or_busy_disk(self):
        selected = {**disk("/dev/sda"), "identity": ("serial-old",)}
        for current in ([], [{**selected, "identity": ("serial-new",)}]):
            with patch.object(installer, "discover", return_value=current), self.assertRaises(installer.Refusal):
                installer.revalidate(selected, 1024)


class StreamSafety(unittest.TestCase):
    def exercise(self, data, expected_size, expected_hash, returncode=0, alter_read=False):
        process = Mock(stdout=io.BytesIO(data))
        process.wait.return_value = returncode
        process.poll.return_value = returncode
        with tempfile.TemporaryFile() as target, patch.object(installer.sp, "Popen", return_value=process):
            if alter_read:
                with patch.object(installer.os, "read", return_value=b"x" * expected_size):
                    installer.write_image(target.fileno(), Path("payload.zst"), expected_size, expected_hash)
            else:
                installer.write_image(target.fileno(), Path("payload.zst"), expected_size, expected_hash)

    def test_valid_stream_and_readback(self):
        self.exercise(b"payload", 7, hashlib.sha256(b"payload").hexdigest())

    def test_decompressor_failure_never_reports_success_even_with_complete_output(self):
        with self.assertRaises(installer.Refusal):
            self.exercise(b"payload", 7, hashlib.sha256(b"payload").hexdigest(), returncode=1)

    def test_short_oversized_or_hash_mismatched_image_fails(self):
        for data, size, sha in ((b"x", 2, "0" * 64), (b"xxx", 2, "0" * 64), (b"xx", 2, "0" * 64)):
            with self.subTest(data=data, size=size), self.assertRaises(installer.Refusal):
                self.exercise(data, size, sha)

    def test_readback_mismatch_fails_before_finalization(self):
        with self.assertRaises(installer.Refusal):
            self.exercise(b"payload", 7, hashlib.sha256(b"payload").hexdigest(), alter_read=True)

    def test_partial_writes_are_retried(self):
        original = os.write
        with patch.object(installer.os, "write", side_effect=lambda fd, data: original(fd, data[:2])):
            self.exercise(b"payload", 7, hashlib.sha256(b"payload").hexdigest())


class CountdownSafety(unittest.TestCase):
    def terminal(self):
        termios = SimpleNamespace(tcgetattr=Mock(return_value=[]), tcsetattr=Mock(), TCSADRAIN=0)
        tty = SimpleNamespace(setcbreak=Mock())
        return patch.dict("sys.modules", {"termios": termios, "tty": tty})

    def test_timeout_only_selects_default_and_does_not_confirm_or_write(self):
        candidates = [disk("/dev/sda"), disk("/dev/sdb")]
        with self.terminal(), patch.object(installer.sys.stdin, "isatty", return_value=True), \
                patch.object(installer.sys.stdin, "fileno", return_value=10), \
                patch.object(installer.time, "monotonic", side_effect=[0, 0, 31]), \
                patch.object(installer.select, "select", return_value=([], [], [])), \
                patch.object(installer, "confirm") as confirmation, patch.object(installer.os, "open") as write:
            self.assertEqual(installer.select_disk(candidates), candidates[0])
            confirmation.assert_not_called()
            write.assert_not_called()

    def test_first_keystroke_cancels_timeout_until_user_finishes_selection(self):
        candidates = [disk("/dev/sda"), disk("/dev/sdb")]
        with self.terminal(), patch.object(installer.sys.stdin, "isatty", return_value=True), \
                patch.object(installer.sys.stdin, "fileno", return_value=10), \
                patch.object(installer.time, "monotonic", return_value=0), \
                patch.object(installer.select, "select", return_value=([10], [], [])) as wait, \
                patch.object(installer.os, "read", side_effect=[b"2", b"\n"]):
            self.assertEqual(installer.select_disk(candidates), candidates[1])
            self.assertEqual([call.args[3] for call in wait.call_args_list], [1, None])

    def test_invalid_input_does_not_reactivate_automatic_disk_selection(self):
        with self.terminal(), patch.object(installer.sys.stdin, "isatty", return_value=True), \
                patch.object(installer.sys.stdin, "fileno", return_value=10), \
                patch.object(installer.time, "monotonic", return_value=0), \
                patch.object(installer.select, "select", return_value=([10], [], [])) as wait, \
                patch.object(installer.os, "read", side_effect=[b"x", b"\n", b"q"]):
            with self.assertRaises(installer.Refusal):
                installer.select_disk([disk("/dev/sda")])
            self.assertEqual([call.args[3] for call in wait.call_args_list], [1, None, None])


class ConfigurationSafety(unittest.TestCase):
    def test_host_check_does_not_create_missing_work_or_release_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {"work_dir": str(root / "not-created/work"),
                      "release_dir": str(root / "not-created/releases"), "minimum_build_free_gib": 80}
            with patch.object(builder.sys, "platform", "linux"), \
                    patch.object(builder.os, "uname", return_value=SimpleNamespace(machine="x86_64"), create=True), \
                    patch.object(builder.shutil, "which", return_value="/usr/bin/tool"), \
                    patch.object(builder.shutil, "disk_usage", return_value=SimpleNamespace(free=100 * builder.GIB)), \
                    patch.object(builder.Path, "is_file", return_value=True), \
                    patch.object(builder.Path, "read_text", return_value="MemAvailable: 8388608 kB\n"):
                result = builder.check_host(config)
            self.assertEqual(result["available_memory_gib"], 8)
            self.assertFalse((root / "not-created").exists())

    def test_check_cli_does_not_read_password_or_start_build(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "build.json"
            config.write_text("{}")
            with patch.object(builder.sys, "argv", ["build.py", "--check", "--config", str(config)]), \
                    patch.object(builder, "check_host", return_value={}) as check, \
                    patch.object(builder, "Build") as build:
                self.assertEqual(builder.main(), 0)
            check.assert_called_once_with({})
            build.assert_not_called()

    def test_secret_parser_does_not_execute_shell_or_expand_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "local.env"
            path.write_text("UBUNTU_INITIAL_PASSWORD='$(touch /tmp/should-not-exist) password'\n")
            self.assertEqual(builder.password_input({}, path), "$(touch /tmp/should-not-exist) password")
            self.assertEqual(builder.password_input({"UBUNTU_INITIAL_PASSWORD": "from-secret"}, path), "from-secret")
            path.write_text("UNEXPECTED=anything\n")
            with self.assertRaises(ValueError):
                builder.password_input({}, path)

    def test_password_validation(self):
        for password in ("", "one\ntwo", "one\rtwo", "zero\x00"):
            with self.subTest(password=password), self.assertRaises(ValueError):
                builder.password_input({"UBUNTU_INITIAL_PASSWORD": password}, Path("nonexistent.env"))

    def test_manifest_rejects_invalid_lengths_layout_and_hashes(self):
        valid = {"format": 1, "architecture": "amd64", "sector_size": 512, "root_partition": 3,
                 "esp_partition": 2, "raw_bytes": 1024, "raw_sha256": "0" * 64,
                 "compressed_sha256": "0" * 64, "build_id": "test-1"}
        installer.validate_manifest(valid)
        for key, value in (("raw_bytes", 0), ("raw_bytes", True), ("raw_bytes", 513), ("sector_size", 4096),
                           ("root_partition", 1), ("raw_sha256", "broken"), ("build_id", "../path")):
            with self.subTest(key=key, value=value), self.assertRaises(installer.Refusal):
                installer.validate_manifest({**valid, key: value})

    def test_workflow_is_manual_only_and_uploads_fixed_small_files(self):
        import yaml
        workflow = yaml.load((ROOT / ".github/workflows/build.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertIn("self-hosted", workflow["jobs"]["build"]["runs-on"])
        uploads = [step for job in workflow["jobs"].values() for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact")]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(set(uploads[0]["with"]["path"].splitlines()), {
            "reports/build-report.json", "reports/build.log", "reports/manifest.json", "reports/packages.txt", "reports/SHA256SUMS"})

    def test_public_report_never_copies_disk_images_raw_logs_or_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            build = builder.Build.__new__(builder.Build)
            build.id = "test"
            build.release = root / "release"
            build.release.mkdir()
            build.work = root / "work"
            build.owns_release = True
            build.stage = root / "stage"
            build.logs = ["safe phase message"]
            build.checks = {}
            build.secret = "sensitive-password"
            build.password_hash = "$6$sensitive-hash"
            for name in ("ubuntu-custom-server.iso", "ubuntu.raw", "tools.log", "local.env"):
                (build.release / name).write_text("must-not-upload")
            with patch.dict(os.environ, {}, clear=True):
                build.report("failed", RuntimeError(build.secret))
            self.assertEqual({p.name for p in build.stage.iterdir()}, {"build-report.json", "build.log"})
            text = "".join(p.read_text() for p in build.stage.iterdir())
            self.assertNotIn(build.secret, text)
            self.assertNotIn(build.password_hash, text)
            self.assertEqual(json.loads((build.stage / "build-report.json").read_text())["manual_acceptance"], "pending")


class WriteGateSafety(unittest.TestCase):
    def test_rejected_confirmation_or_changed_disk_never_opens_target(self):
        manifest = {"format": 1, "architecture": "amd64", "sector_size": 512, "root_partition": 3,
                    "esp_partition": 2, "raw_bytes": 1024, "raw_sha256": "0" * 64,
                    "compressed_sha256": "0" * 64, "build_id": "test"}
        for reject_at in ("confirm", "revalidate"):
            with self.subTest(reject_at=reject_at), tempfile.TemporaryDirectory() as folder:
                media = Path(folder)
                payload = media / "payload"
                payload.mkdir()
                (payload / "manifest.json").write_text(json.dumps(manifest))
                (media / "UBUNTU_CUSTOMIZER_MEDIA").write_text("test\n")
                with patch.object(installer, "MEDIA", media), patch.object(installer, "PAYLOAD", payload), \
                        patch.object(installer.os, "geteuid", return_value=0, create=True), \
                        patch.object(installer, "sha256_file", return_value="0" * 64), \
                        patch.object(installer.sp, "run"), patch.object(installer, "discover", return_value=[disk("/dev/sda")]), \
                        patch.object(installer, "select_disk", return_value=disk("/dev/sda")), \
                        patch.object(installer, "confirm", side_effect=installer.Refusal("cancelled") if reject_at == "confirm" else None) as confirmation, \
                        patch.object(installer, "revalidate", side_effect=installer.Refusal("changed")) as validate, \
                        patch.object(installer.os, "open") as open_target, patch.object(installer, "write_image") as write:
                    with self.assertRaises(installer.Refusal):
                        installer.main()
                    confirmation.assert_called_once()
                    open_target.assert_not_called()
                    write.assert_not_called()
                    if reject_at == "confirm":
                        validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

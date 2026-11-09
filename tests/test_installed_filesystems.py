"""Installed Ubuntu must discover both filesystems before installation succeeds."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name != "nt" else shutil.which("bash", path=r"C:\Program Files\Git\bin")
SCRIPT = (ROOT / "installer/finalize.sh").read_text()
CHECK = SCRIPT.split("<<'FILESYSTEM_CHECK'\n", 1)[1].split("\nFILESYSTEM_CHECK", 1)[0]


@unittest.skipUnless(BASH, "Bash is required for target filesystem checks")
class InstalledFilesystems(unittest.TestCase):
    def check(self, *, root_status=0, esp_status=0, root_uuid="root-new", esp_uuid="efi-new", root_type="ext4"):
        stub = r'''
blkid() {
    [[ $1 == -p && $2 == -o && $3 == export ]] || return 99
    if [[ $4 == /dev/testdisk3 ]]; then
        printf 'TYPE=%s\nUUID=%s\n' "$TEST_ROOT_TYPE" "$TEST_ROOT_UUID"
        return "$TEST_ROOT_STATUS"
    fi
    [[ $4 == /dev/testdisk2 ]] || return 99
    printf 'TYPE=vfat\nUUID=%s\n' "$TEST_ESP_UUID"
    return "$TEST_ESP_STATUS"
}
'''
        env = {**os.environ, "TEST_ROOT_STATUS": str(root_status), "TEST_ESP_STATUS": str(esp_status),
               "TEST_ROOT_UUID": root_uuid, "TEST_ESP_UUID": esp_uuid, "TEST_ROOT_TYPE": root_type}
        return subprocess.run([BASH, "-s", "--", "/dev/testdisk3", "root-new", "/dev/testdisk2", "efi-new"],
                              input=stub + CHECK + "\necho 'filesystem checks passed'\n", text=True,
                              capture_output=True, env=env, timeout=10)

    def test_unambiguous_filesystems_and_matching_uuids_pass(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("filesystem checks passed", result.stdout)

    def test_ambiguous_or_missing_root_refuses_even_if_probe_prints_ext4(self):
        for status in (2, 8):
            with self.subTest(status=status):
                result = self.check(root_status=status)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("possible conflicting signatures", result.stderr)
                self.assertNotIn("checks passed", result.stdout)

    def test_wrong_type_uuid_or_ambiguous_efi_refuses(self):
        for options in ({"root_uuid": "old"}, {"esp_uuid": "old"}, {"root_type": "btrfs"}, {"esp_status": 8}):
            with self.subTest(options=options):
                self.assertNotEqual(self.check(**options).returncode, 0)


@unittest.skipUnless(BASH, "Bash is required for finalization diagnostics")
class FinalizationDiagnostics(unittest.TestCase):
    SETUP = SCRIPT.split("[[ ${UBUNTU_CUSTOMIZER_WRITE_VERIFIED", 1)[0]

    def test_command_and_pipeline_failures_report_stage_and_preserve_exit_code(self):
        for command, status in (("false", 1), ("(exit 7) | cat", 7),
                                ("probe() { return 9; }; probe", 9)):
            with self.subTest(command=command):
                result = subprocess.run([BASH, "-s"], input=self.SETUP +
                                        "\nfinalize_stage='Test partition refresh'\n" + command +
                                        "\necho SHOULD_NOT_RUN\n", text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, status)
                self.assertIn("Finalization failed: Test partition refresh", result.stderr)
                self.assertIn(f"exit {status}", result.stderr)
                self.assertNotIn("SHOULD_NOT_RUN", result.stdout)

    def test_corrected_e2fsck_status_is_accepted_and_fatal_status_stops_resize(self):
        check = SCRIPT.split("check_status=0\n", 1)[1].split('resize2fs "$root"', 1)[0]
        for status in (0, 1, 2, 4, 8):
            with self.subTest(status=status):
                result = subprocess.run([BASH, "-s"], input=self.SETUP +
                                        f"\nroot=/dev/test-root\ne2fsck() {{ return {status}; }}\n" +
                                        "check_status=0\n" + check + "echo RESIZE_ALLOWED\n",
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0 if status <= 1 else status)
                self.assertEqual("RESIZE_ALLOWED" in result.stdout, status <= 1)
                if status > 1:
                    self.assertIn("Root filesystem check failed", result.stderr)

    def test_explicit_exit_and_shell_errors_save_stage_and_command_for_python_report(self):
        for command, status in (("exit 4", 4), ("false", 1)):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as folder:
                stage_file = Path(folder) / "stage.txt"
                error_file = Path(folder) / "error.txt"
                env = {**os.environ, "UBUNTU_CUSTOMIZER_STAGE_FILE": stage_file.as_posix(),
                       "UBUNTU_CUSTOMIZER_ERROR_FILE": error_file.as_posix()}
                result = subprocess.run([BASH, "-s"], input=self.SETUP +
                                        "\nfinalize_stage='Partition check'\nreport_stage\n" + command,
                                        env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(stage_file.read_text().strip(), "Partition check")
                self.assertIn(f"exit {status}", error_file.read_text())
                self.assertIn(command, error_file.read_text())

    def test_new_uuids_are_probed_without_cache_and_missing_values_stop_before_mount(self):
        check = SCRIPT.split('root_uuid=$(blkid -p', 1)[1].split('mount "$root" "$target"', 1)[0]
        check = 'root_uuid=$(blkid -p' + check
        stub = r'''
root=/dev/test-root
esp=/dev/test-esp
blkid() {
    [[ $1 == -p && $2 == -s && $3 == UUID && $4 == -o && $5 == value ]] || return 99
    if [[ $6 == "$root" ]]; then
        printf '%s\n' "$TEST_ROOT_UUID"
    else
        printf '%s\n' "$TEST_ESP_UUID"
    fi
}
'''
        for root_uuid, esp_uuid, passes in (("12345678-1234-4321-9876-123456789abc", "ABCD-1234", True),
                                            ("", "ABCD-1234", False),
                                            ("12345678-1234-4321-9876-123456789abc", "", False)):
            with self.subTest(root_uuid=root_uuid, esp_uuid=esp_uuid):
                result = subprocess.run([BASH, "-s"], input=self.SETUP + stub + check + "echo MOUNT_ALLOWED\n",
                                        env={**os.environ, "TEST_ROOT_UUID": root_uuid, "TEST_ESP_UUID": esp_uuid},
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode == 0, passes, result.stderr)
                self.assertEqual("MOUNT_ALLOWED" in result.stdout, passes)


if __name__ == "__main__":
    unittest.main()

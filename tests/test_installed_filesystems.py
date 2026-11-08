"""Installed Ubuntu must discover both filesystems before installation succeeds."""
import os
from pathlib import Path
import shutil
import subprocess
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


if __name__ == "__main__":
    unittest.main()

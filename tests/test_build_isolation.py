"""Validate namespace guards and exercise the real launcher with harmless stubs."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("isolation_builder", ROOT / "image_builder/pipeline.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
BASH = shutil.which("bash") if os.name != "nt" else shutil.which("bash", path=r"C:\Program Files\Git\bin")


class NamespaceGuard(unittest.TestCase):
    def test_all_private_namespaces_and_matching_procfs_pass(self):
        originals = {"UBUNTU_CUSTOMIZER_HOST_" + name.upper(): "host-" + name
                     for name in ("mnt", "pid", "uts", "ipc")}
        with patch.dict(os.environ, originals, clear=True), \
                patch.object(builder.os, "readlink", return_value="private-namespace"), \
                patch.object(builder.os, "getpid", return_value=1), \
                patch.object(builder.Path, "exists", return_value=True):
            builder.check_build_isolation()

    def test_shared_or_unset_namespace_is_rejected(self):
        for name in ("mnt", "pid", "uts", "ipc"):
            for missing in (False, True):
                with self.subTest(namespace=name, missing=missing):
                    originals = {"UBUNTU_CUSTOMIZER_HOST_" + item.upper(): "host-" + item
                                 for item in ("mnt", "pid", "uts", "ipc")}
                    if missing:
                        originals.pop("UBUNTU_CUSTOMIZER_HOST_" + name.upper())

                    def namespace_value(path):
                        return "host-" + name if path.endswith("/" + name) else "private-namespace"

                    with patch.dict(os.environ, originals, clear=True), \
                            patch.object(builder.os, "readlink", side_effect=namespace_value), \
                            self.assertRaisesRegex(RuntimeError, "Private " + name + " namespace required"):
                        builder.check_build_isolation()

    def test_wrong_pid_or_procfs_is_rejected(self):
        originals = {"UBUNTU_CUSTOMIZER_HOST_" + name.upper(): "host-" + name
                     for name in ("mnt", "pid", "uts", "ipc")}
        for pid, exists in ((1234, True), (1, False)):
            with self.subTest(pid=pid, procfs=exists), patch.dict(os.environ, originals, clear=True), \
                    patch.object(builder.os, "readlink", return_value="private-namespace"), \
                    patch.object(builder.os, "getpid", return_value=pid), \
                    patch.object(builder.Path, "exists", return_value=exists), \
                    self.assertRaisesRegex(RuntimeError, "matching procfs required"):
                builder.check_build_isolation()


@unittest.skipUnless(BASH, "Bash is required for launcher regression")
class IsolationLauncher(unittest.TestCase):
    def test_launcher_exports_parent_namespaces_and_forwards_all_isolation_flags(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "build.sh").write_text((ROOT / "scripts/build.sh").read_text())
            stub = '''
id() { echo 0; }
readlink() { echo "host-${1##*/}"; }
unshare() {
    [[ $UBUNTU_CUSTOMIZER_HOST_MNT == host-mnt ]]
    [[ $UBUNTU_CUSTOMIZER_HOST_PID == host-pid ]]
    [[ $UBUNTU_CUSTOMIZER_HOST_UTS == host-uts ]]
    [[ $UBUNTU_CUSTOMIZER_HOST_IPC == host-ipc ]]
    printf '%s\\n' "$@"
}
export -f id readlink unshare
exec() { "$@"; }
'''
            result = subprocess.run([BASH, "-c", stub + '\nsource "$1" --build-id namespace-test',
                                     "test", str(root / "build.sh")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = result.stdout.splitlines()
            self.assertEqual(arguments[:9], ["--mount", "--pid", "--fork", "--kill-child=SIGKILL",
                                             "--mount-proc", "--uts", "--ipc", "--propagation", "private"])
            self.assertIn("python3", arguments)
            self.assertEqual(arguments[-2:], ["--build-id", "namespace-test"])


if __name__ == "__main__":
    unittest.main()

"""Exercise Live error reporting using harmless processes and temporary files."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reporting_installer", ROOT / "installer/installer.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerReporting(unittest.TestCase):
    def test_capture_keeps_python_and_child_stdout_stderr_and_restores_console(self):
        with tempfile.TemporaryDirectory() as folder:
            code = '''
import importlib.util, os, pathlib, subprocess, sys
spec = importlib.util.spec_from_file_location("installer", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
report = module.InstallationReport(pathlib.Path(sys.argv[2]))
stdin_fd = sys.stdin.fileno()
stdin_tty = sys.stdin.isatty()
with report.capture():
    print("PYTHON-OUT", flush=True)
    print("PYTHON-ERR", file=sys.stderr, flush=True)
    subprocess.run([sys.executable, "-c", "import os; os.write(1, b'CHILD-OUT\\\\n'); os.write(2, b'CHILD-ERR\\\\n')"], check=True)
assert sys.stdin.fileno() == stdin_fd and sys.stdin.isatty() == stdin_tty
print("RESTORED")
'''
            result = subprocess.run([sys.executable, "-c", code, str(ROOT / "installer/installer.py"), folder],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            log = next(Path(folder).glob("*/installation.log")).read_text()
            for marker in ("PYTHON-OUT", "PYTHON-ERR", "CHILD-OUT", "CHILD-ERR"):
                self.assertIn(marker, log)
                self.assertIn(marker, result.stdout)
            self.assertIn("RESTORED", result.stdout)
            self.assertNotIn("RESTORED", log)

    def test_failure_shows_detailed_shell_error_and_tail_and_saves_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            report = installer.InstallationReport(Path(folder))
            report.log_path.write_text("Tool output\n")
            report.stage_path.write_text("Locating partition devices")
            report.command_path.write_text("line 47, exit 1: [[ -b $root ]]\n")
            report.disk = "/dev/nvme0n1"
            report.writes_started = True
            report.recent.append("Missing root partition device\n")

            def probe(command, **kwargs):
                if command[:2] == ["lsblk", "--json"]:
                    value = json.dumps({"blockdevices": [{"path": report.disk, "type": "disk", "children": [
                        {"path": "/dev/nvme0n1p3", "type": "part"}]}]})
                else:
                    value = "diagnostic output"
                return subprocess.CompletedProcess(command, 0, value)

            with patch.object(installer.sp, "run", side_effect=probe) as run, \
                    patch.object(installer.sys, "stdout", io.StringIO()) as console:
                report.failure(subprocess.CalledProcessError(1, ["bash", "finalize.sh"]))
            screen = console.getvalue()
            for text in ("[ERROR] INSTALLATION STOPPED", "Locating partition devices", "line 47, exit 1",
                         "Missing root partition device", str(report.log_path), "Live RAM"):
                self.assertIn(text, screen)
            self.assertIn("Missing root partition device", report.summary_path.read_text())
            log = report.log_path.read_text()
            self.assertIn("Automatic read-only diagnostics", log)
            self.assertIn("blkid -p -- /dev/nvme0n1p3", log)
            self.assertIn("wipefs --no-act -- /dev/nvme0n1p3", log)
            for call in run.call_args_list:
                self.assertLessEqual(call.kwargs["timeout"], 5)

    def test_probe_stderr_survives_capture_and_reporting_commands_never_erase(self):
        with patch.object(installer.sp, "run", return_value=subprocess.CompletedProcess(
                ["lsblk"], 32, "", "lsblk: read failure\n")), \
                patch.object(installer.sys, "stderr", io.StringIO()) as console:
            with self.assertRaises(subprocess.CalledProcessError):
                installer.output("lsblk")
        self.assertIn("lsblk: read failure", console.getvalue())

    def test_entry_point_saves_traceback_and_prints_summary_without_running_real_installer(self):
        with tempfile.TemporaryDirectory() as folder:
            code = '''
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("installer", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
report_type = module.InstallationReport
module.InstallationReport = lambda: report_type(pathlib.Path(sys.argv[2]))
def fail():
    module.installation_stage("Checking test disk")
    print("Original tool failure", flush=True)
    raise RuntimeError("test command failed")
module.main = fail
report_type.diagnostics = lambda self: None
sys.exit(module.run_installer())
'''
            result = subprocess.run([sys.executable, "-c", code, str(ROOT / "installer/installer.py"), folder],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("[ERROR] INSTALLATION STOPPED", result.stdout)
            self.assertIn("Stage: Checking test disk", result.stdout)
            self.assertIn("Original tool failure", result.stdout)
            self.assertNotIn("INSTALLATION COMPLETE", result.stdout)
            log = next(Path(folder).glob("*/installation.log")).read_text()
            self.assertIn("Traceback (most recent call last)", log)
            self.assertIn("RuntimeError: test command failed", log)
            self.assertTrue(next(Path(folder).glob("*/error.txt")).is_file())

    def test_diagnostic_failure_does_not_hide_original_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            report = installer.InstallationReport(Path(folder))
            with patch.object(report, "diagnostics", side_effect=OSError("unavailable")), \
                    patch.object(installer.sys, "stdout", io.StringIO()) as console, \
                    patch.object(installer.sys, "stderr", io.StringIO()):
                report.failure(ValueError("Original fault"))
            self.assertIn("Original fault", console.getvalue())
            self.assertIn("[ERROR] INSTALLATION STOPPED", console.getvalue())


if __name__ == "__main__":
    unittest.main()

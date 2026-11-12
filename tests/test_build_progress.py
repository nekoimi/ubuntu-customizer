"""Progress must keep subprocess behavior and private output boundaries intact."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("progress_builder", ROOT / "image_builder/pipeline.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuildProgress(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.build = builder.Build.__new__(builder.Build)
        self.build.work = Path(folder.name)
        self.build.logs = []

    def test_real_subprocess_keeps_input_and_output_private(self):
        console = io.StringIO()
        with redirect_stdout(console):
            result = self.build.run([sys.executable, "-c", "import sys; print(sys.stdin.read()); print('private stderr', file=sys.stderr)"],
                                    label="Installing packages", input=b"private password")
        self.assertEqual(result.returncode, 0)
        raw = (self.build.work / "tools.log").read_text()
        self.assertIn("private password", raw)
        self.assertIn("private stderr", raw)
        self.assertIn("Starting: Installing packages", console.getvalue())
        self.assertIn("Completed: Installing packages", console.getvalue())
        self.assertNotIn("private", console.getvalue())
        self.assertNotIn("private", "\n".join(self.build.logs))

    def test_subprocess_failure_is_reported_and_preserves_exit_code(self):
        console = io.StringIO()
        with redirect_stdout(console), self.assertRaises(subprocess.CalledProcessError) as raised:
            self.build.run([sys.executable, "-c", "raise SystemExit(7)"], label="Checking packages")
        self.assertEqual(raised.exception.returncode, 7)
        self.assertIn("Failed: Checking packages", console.getvalue())
        self.assertIn("Command failed: Checking packages; exit code 7", console.getvalue())
        self.assertNotIn("Completed:", console.getvalue())

    def test_build_failure_shows_tool_exit_code_and_exact_log_path_without_arguments(self):
        build = Mock(work=self.build.work, loop=None)
        build.system_image.side_effect = subprocess.CalledProcessError(100, ["debootstrap", "private-argument"])
        console = io.StringIO()
        with patch.object(builder.sys, "argv", ["build.py"]), \
                patch.object(builder, "Build", return_value=build), \
                patch.object(builder.signal, "signal"), \
                patch.object(builder.os, "umask"), redirect_stderr(console):
            self.assertEqual(builder.main(), 1)
        self.assertIn("debootstrap; exit code 100", console.getvalue())
        self.assertIn(str(build.work / "tools.log"), console.getvalue())
        self.assertNotIn("private-argument", console.getvalue())
        build.report.assert_called_once_with("failed", build.system_image.side_effect)
        build.unmount_to.assert_called_once_with(0)

    def test_heartbeat_stops_on_success_and_interruption(self):
        real_thread = threading.Thread
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                heartbeat = threading.Event()
                threads = []
                messages = []

                def note(message):
                    messages.append(message)
                    if message.startswith("Still running:"):
                        heartbeat.set()

                def make_thread(*args, **kwargs):
                    thread = real_thread(*args, **kwargs)
                    threads.append(thread)
                    return thread

                with patch.object(builder, "PROGRESS_INTERVAL", 0.01), \
                        patch.object(self.build, "note", side_effect=note), \
                        patch.object(builder.threading, "Thread", side_effect=make_thread):
                    try:
                        with self.build.progress("Long operation"):
                            self.assertTrue(heartbeat.wait(2), "No progress heartbeat received")
                            if interrupted:
                                raise KeyboardInterrupt
                    except KeyboardInterrupt:
                        self.assertTrue(interrupted)
                self.assertTrue(threads)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertTrue(messages[-1].startswith("Failed:" if interrupted else "Completed:"))


if __name__ == "__main__":
    unittest.main()

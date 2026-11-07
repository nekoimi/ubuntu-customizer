"""Exercise guest configuration checks with output large enough to trigger SIGPIPE."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name != "nt" else shutil.which("bash", path=r"C:\Program Files\Git\bin")
SCRIPT = (ROOT / "scripts/check-guest.sh").read_text()
CHECKS = SCRIPT[SCRIPT.index("sshd -t\n"):SCRIPT.index("\ngrep -q '^Signed-By:")]
TRAP = next(line for line in SCRIPT.splitlines() if line.startswith("trap "))
TMUX_CHECK = SCRIPT.split("<<'TMUX_CHECK'\n", 1)[1].split("\nTMUX_CHECK", 1)[0]


@unittest.skipUnless(BASH, "Bash is required for guest check regressions")
class GuestConfigurationChecks(unittest.TestCase):
    def run_checks(self, *, permit_root="no", ssh_status=0, apt_status=0):
        # Everything is a shell stub; no guest or host services/files are modified.
        stub = r'''
set -euo pipefail
emit_padding() {
    for ((i=0; i<3000; i++)); do
        printf '%s\n' 'padding-output-to-force-a-full-pipe-012345678901234567890123456789'
    done
}
sshd() {
    if [[ $1 == -t ]]; then return 0; fi
    printf 'permitrootlogin %s\n' "$TEST_PERMIT_ROOT"
    printf '%s\n' 'passwordauthentication yes' 'printmotd no' 'printlastlog no' 'banner none' 'clientaliveinterval 60' 'clientalivecountmax 3'
    emit_padding
    return "$TEST_SSH_STATUS"
}
apt-config() {
    printf '%s\n' 'APT::Periodic::Unattended-Upgrade "0";' 'Unattended-Upgrade::Automatic-Reboot "false";'
    emit_padding
    return "$TEST_APT_STATUS"
}
'''
        env = {**os.environ, "TEST_PERMIT_ROOT": permit_root, "TEST_SSH_STATUS": str(ssh_status),
               "TEST_APT_STATUS": str(apt_status)}
        return subprocess.run([BASH, "-s"], input=stub + "\n" + TRAP + "\n" + CHECKS,
                              text=True, capture_output=True, env=env)

    def test_large_valid_configuration_passes_without_broken_pipe(self):
        result = self.run_checks()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrong_ssh_policy_still_fails_with_diagnostic(self):
        result = self.run_checks(permit_root="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Guest validation failed", result.stderr)
        self.assertIn("permitrootlogin no", result.stderr)

    def test_sshd_failure_is_not_hidden_by_matching_output(self):
        result = self.run_checks(ssh_status=7)
        self.assertEqual(result.returncode, 7)
        self.assertIn("sshd_config=$(sshd -T)", result.stderr)

    def test_apt_config_failure_is_not_hidden_by_matching_output(self):
        result = self.run_checks(apt_status=8)
        self.assertEqual(result.returncode, 8)
        self.assertIn("apt_configuration=$(apt-config dump)", result.stderr)

    def test_tmux_default_startup_checks_mouse_and_always_cleans_up(self):
        for mouse in ("on", "off"):
            stub = r'''
tmux() {
    case "$*" in
        '-L customizer-check new-session -d -s check') return 0 ;;
        '-L customizer-check show-options -gv mouse') printf '%s\n' "$TEST_MOUSE" ;;
        '-L customizer-check show-options -gv history-limit') echo 50000 ;;
        '-L customizer-check show-options -gv base-index') echo 1 ;;
        '-L customizer-check kill-server') echo 'tmux test server cleaned up' ;;
        *) echo 'unexpected tmux invocation' >&2; return 2 ;;
    esac
}
'''
            with self.subTest(mouse=mouse):
                result = subprocess.run([BASH, "-s"], input=stub + TMUX_CHECK,
                                        text=True, capture_output=True,
                                        env={**os.environ, "TEST_MOUSE": mouse})
                self.assertEqual(result.returncode, 0 if mouse == "on" else 1, result.stderr)
                self.assertIn("tmux test server cleaned up", result.stdout)


if __name__ == "__main__":
    unittest.main()

"""Exercise the build's UFW setup without invoking real firewall utilities."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name != "nt" else shutil.which("bash", path=r"C:\Program Files\Git\bin")
CUSTOMIZE = (ROOT / "scripts/customize.sh").read_text()
SETUP = CUSTOMIZE.split("<<'FIREWALL_CONFIG'\n", 1)[1].split("\nFIREWALL_CONFIG", 1)[0]


@unittest.skipUnless(BASH, "Bash is required for firewall regressions")
class FirewallSetup(unittest.TestCase):
    def configure(self, *, original="net:[100]", current="net:[200]", allow_status=0, active="active"):
        stubs = r'''
readlink() { printf '%s\n' "$TEST_CURRENT_NETNS"; }
sed() { printf 'CONFIG_EDIT: %s\n' "$*" >&2; }
ufw() {
    printf 'UFW: %s\n' "$*" >&2
    case "$*" in
        'allow 22/tcp') return "$TEST_ALLOW_STATUS" ;;
        'status verbose')
            printf 'Status: %s\n' "$TEST_ACTIVE"
            echo 'Default: deny (incoming), allow (outgoing), deny (routed)'
            ;;
    esac
}
'''
        env = {**os.environ, "UBUNTU_CUSTOMIZER_ORIGINAL_NETNS": original,
               "TEST_CURRENT_NETNS": current, "TEST_ALLOW_STATUS": str(allow_status), "TEST_ACTIVE": active}
        return subprocess.run([BASH, "-s"], input=stubs + SETUP, env=env,
                              text=True, capture_output=True)

    def test_unisolated_or_missing_namespace_refuses_before_configuration(self):
        for original in ("", "net:[200]"):
            with self.subTest(original=original):
                result = self.configure(original=original)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing to configure UFW", result.stderr)
                self.assertNotIn("UFW:", result.stderr)
                self.assertNotIn("CONFIG_EDIT:", result.stderr)

    def test_allow_ssh_is_saved_before_firewall_is_enabled(self):
        result = self.configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [line for line in result.stderr.splitlines() if line.startswith("UFW:")]
        self.assertEqual(calls, ["UFW: default deny incoming", "UFW: default allow outgoing",
                                 "UFW: default deny routed", "UFW: allow 22/tcp",
                                 "UFW: allow 2017/tcp",
                                 "UFW: --force enable", "UFW: status verbose"])

    def test_ssh_rule_failure_does_not_enable_firewall(self):
        result = self.configure(allow_status=7)
        self.assertEqual(result.returncode, 7)
        self.assertNotIn("UFW: --force enable", result.stderr)

    def test_inactive_firewall_cannot_pass_validation(self):
        self.assertNotEqual(self.configure(active="inactive").returncode, 0)

    def test_namespace_creation_failure_stops_without_running_firewall_commands(self):
        wrapper = CUSTOMIZE[CUSTOMIZE.index("original_netns=$(readlink /proc/self/ns/net)"):
                            CUSTOMIZE.index("echo 'Configuring enabled services and automatic update policy'")]
        stubs = r'''
set -euo pipefail
readlink() { echo 'net:[100]'; }
ufw() { echo 'unexpected firewall command' >&2; return 99; }
unshare() {
    [[ $1 == --net && $2 == -- && $3 == bash && $4 == -se ]]
    return 19
}
'''
        result = subprocess.run([BASH, "-s"], input=stubs + wrapper + "\necho 'unsafe fallback'\n",
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertNotIn("unexpected firewall command", result.stderr)
        self.assertNotIn("unsafe fallback", result.stdout)


if __name__ == "__main__":
    unittest.main()

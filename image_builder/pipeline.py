#!/usr/bin/env python3
"""Build with private mount, PID, UTS and IPC namespaces using file-backed disks."""
import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess as sp
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import urllib.error
import urllib.parse

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'scripts'))
from build_env import configure_proxy, password_input, proxy_secrets, ntfy_settings, NTFY_KEYS
from image_builder.config import load_config
from image_builder.distros import target_for as target_for  # Compatibility for helper callers.

GIB = 1024 ** 3
PROGRESS_INTERVAL = 30
ALPINE_FALLBACK_MIRRORS = ["https://mirrors.tuna.tsinghua.edu.cn/alpine"]


from image_builder.core import (alpine_mirrors, package_cache_directory, check_build_isolation,
                                wait_loop_partitions, download, http_error_summary,
                                proxy_summary, check_host)
from image_builder.packaging import AlpineISO


class Build(AlpineISO):
    def __init__(self, args):
        self.config, self.target = load_config(args.config, getattr(args, 'profile', None))
        self.id = args.build_id
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,90}", self.id):
            raise ValueError("Invalid build id")
        for key, minimum, maximum in (("template_gib", 16, 128), ("compression_threads", 1, 16), ("minimum_build_free_gib", 40, 1024)):
            value = self.config[key]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("Invalid resource setting: " + key)
        for key in ("system_mirror", "docker_mirror", "python_index", "alpine_mirror"):
            if not self.config[key].startswith("https://") or any(c.isspace() for c in self.config[key]):
                raise ValueError("HTTPS URL required: " + key)
        alpine_mirrors(self.config)
        if not Path(self.config["work_dir"]).is_absolute() or not Path(self.config["release_dir"]).is_absolute():
            raise ValueError("Absolute persistent paths required")
        self.work_base = Path(self.config["work_dir"]).resolve()
        self.release_base = Path(self.config["release_dir"]).resolve()
        self.cache_base = package_cache_directory(self.config)
        for path in (self.work_base, self.release_base):
            if not path.is_absolute() or path == Path("/") or path == REPO or REPO in path.parents:
                raise ValueError("Use persistent work/release directories outside the checkout")
        if self.work_base == self.release_base or self.work_base in self.release_base.parents or self.release_base in self.work_base.parents:
            raise ValueError("Work and release directories must be separate")
        self.work = self.work_base / self.id
        self.release = self.release_base / self.id
        self.stage = args.reports.resolve()
        if self.stage != REPO / "reports":
            raise ValueError("Public report staging must be checkout/reports")
        self.mounts = []
        self.loop = None
        self.owns_release = False
        self.logs = []
        self.checks = {}
        self.details = {"build_id": self.id, "architecture": "amd64", "distro": self.target.name, "version": self.target.version, "username": self.config["username"], "manual_acceptance": "pending"}
        self.details['components'] = self.config['components']
        self.proxy_env = configure_proxy()
        self.ntfy_config = ntfy_settings(os.environ)
        for key in NTFY_KEYS:
            os.environ.pop(key, None)
        self.secret = password_input(os.environ, REPO / "config/local.env")
        self.password_hash = sp.check_output(["openssl", "passwd", "-6", "-stdin"], input=self.secret.encode()).decode().strip()
        os.environ.pop("UBUNTU_INITIAL_PASSWORD", None)
        os.environ.pop("INITIAL_PASSWORD", None)

    def note(self, message):
        line = dt.datetime.now(dt.timezone.utc).isoformat() + " " + message
        self.logs.append(line)
        print(line, flush=True)

    @contextmanager
    def progress(self, label, *, announce=True):
        """Report elapsed time without exposing tool output, arguments or stdin."""
        started = time.monotonic()
        stopped = threading.Event()
        if announce:
            self.note("Starting: " + label)

        def heartbeat():
            while not stopped.wait(PROGRESS_INTERVAL):
                self.note(f"Still running: {label} ({time.monotonic() - started:.0f}s elapsed; details in tools.log)")

        ticker = threading.Thread(target=heartbeat, daemon=True)
        ticker.start()
        outcome = "Completed"
        try:
            yield
        except BaseException:
            outcome = "Failed"
            raise
        finally:
            stopped.set()
            ticker.join()
            elapsed = time.monotonic() - started
            if announce or elapsed >= PROGRESS_INTERVAL:
                self.note(f"{outcome}: {label} ({elapsed:.1f}s)")

    def run(self, argv, *, label=None, **kwargs):
        # Raw tool output is local only, never streamed to Actions.
        with (self.work / "tools.log").open("ab") as log:
            log.write(("\n$ " + shlex.join(str(value) for value in argv) + "\n").encode())
            log.flush()
            with self.progress(label or Path(str(argv[0])).name, announce=label is not None):
                try:
                    return sp.run([str(v) for v in argv], stdout=log, stderr=sp.STDOUT, check=True, **kwargs)
                except sp.CalledProcessError as error:
                    self.note(f"Command failed: {label or Path(str(argv[0])).name}; exit code {error.returncode}")
                    raise

    def output(self, argv):
        return sp.check_output([str(v) for v in argv], text=True).strip()

    def mount(self, args, target):
        target.mkdir(parents=True, exist_ok=True)
        self.run(["mount", *args, target])
        self.mounts.append(target)

    def api_mounts(self, root):
        self.mount(["--rbind", "/dev"], root / "dev")
        self.run(["mount", "--make-rslave", root / "dev"])
        self.mount(["-t", "proc", "proc"], root / "proc")
        self.mount(["--rbind", "/sys"], root / "sys")
        self.run(["mount", "--make-rslave", root / "sys"])
        self.mount(["-t", "tmpfs", "tmpfs"], root / "run")
        resolver = root / "etc/resolv.conf"
        resolver.unlink(missing_ok=True)
        shutil.copyfile("/etc/resolv.conf", resolver)

    def unmount_to(self, count):
        while len(self.mounts) > count:
            target = self.mounts[-1]
            self.run(["umount", "--recursive", target])
            self.mounts.pop()

    def guest(self, root, script, environment=None, *, label=None):
        command = ["chroot", root, "/usr/bin/env", "-i", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/root", "LC_ALL=C.UTF-8", "UBUNTU_CUSTOMIZER_GUEST=1"]
        command += [key + "=" + value for key, value in (environment or {}).items()]
        command += ["/bin/bash", "-se"]
        # stdin is never logged; proxy credentials must not appear in command arguments.
        exports = "".join("export " + key + "=" + shlex.quote(value) + "\n"
                          for key, value in self.proxy_env.items())
        self.run(command, label=label, input=(exports + script).encode())

    def preflight(self):
        import fcntl
        if sys.platform != "linux" or os.geteuid() != 0 or os.uname().machine != "x86_64":
            raise RuntimeError("Run build.sh as root on Linux x86_64")
        check_build_isolation()
        check_host(self.config)
        for path in (self.work_base, self.release_base):
            path.mkdir(parents=True, exist_ok=True)
        self.lock = (self.work_base / ".build.lock").open("w")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if self.cache_base is not None:
            self.cache_base.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.cache_lock = (self.cache_base / ".cache.lock").open("a")
            fcntl.flock(self.cache_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.work.mkdir(mode=0o700)
        self.release.mkdir(mode=0o755)
        self.owns_release = True
        self.stage.mkdir(exist_ok=True)
        # Only fixed report names may be overwritten; never recursively delete arbitrary paths.
        for name in ("build-report.json", "build.log", "manifest.json", "packages.txt", "SHA256SUMS"):
            (self.stage / name).unlink(missing_ok=True)
        self.note("Build ID: " + self.id)
        self.note("Detailed log: " + str(self.work / "tools.log"))
        self.note("Preflight passed; private mount/PID/UTS/IPC namespaces; file-backed disks only")
        for line in proxy_summary(self.proxy_env):
            self.note(line)
        self.checks["preflight"] = "passed"

    def package_cache(self):
        if self.cache_base is None:
            return None
        sources = {key: self.config.get(key, '') for key in ('system_mirror', 'security_mirror', 'docker_mirror')}
        repository_id = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()[:12]
        cache = self.cache_base / self.target.name / self.target.suite / 'amd64' / repository_id / 'apt/archives'
        cache.mkdir(parents=True, exist_ok=True)
        packages = list(cache.glob("*.deb"))
        size = sum(path.stat().st_size for path in packages)
        self.note(f"Persistent APT cache: {len(packages)} packages, {size / GIB:.2f} GiB; signed repository metadata still refreshed")
        return cache

    def clean_system(self, root, cache_mount_count):
        # Never apt-get clean a bind-mounted persistent package cache.
        self.unmount_to(cache_mount_count)
        self.guest(root, """rm -f /etc/apt/apt.conf.d/99-customizer-build-downloads
apt-get clean
rm -rf /tmp/customizer /root/.cache /root/.ssh /home/${TARGET_USER}/.cache /home/${TARGET_USER}/.ssh /var/lib/tailscale /var/lib/docker /var/lib/containerd
rm -f /etc/ssh/ssh_host_* /var/lib/systemd/random-seed /root/.bash_history /home/${TARGET_USER}/.bash_history
find /var/log -type f -exec truncate -s 0 {} +
find /var/lib/apt/lists -type f ! -name lock -delete
rm -f /etc/machine-id /var/lib/dbus/machine-id
: > /etc/machine-id
ln -s /etc/machine-id /var/lib/dbus/machine-id
rm -f /etc/resolv.conf
ln -s /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
""", {"TARGET_USER": self.config["username"]})

    def bootstrap_system(self, root, apt_cache):
        self.target.bootstrap(self, root, apt_cache)

    def system_image(self):
        self.note(f"Building {self.target.name} {self.target.version} disk image using signed APT repositories")
        raw = self.work / "system.raw"
        with raw.open("xb") as stream:
            stream.truncate(int(self.config["template_gib"]) * GIB)
        self.run(["sgdisk", "--clear", "--new=1:2048:+2M", "--typecode=1:ef02", "--new=2:0:+512M", "--typecode=2:ef00", "--new=3:0:0", "--typecode=3:8300", raw])
        self.loop = self.output(["losetup", "--find", "--show", "--partscan", raw])
        if not re.fullmatch(r"/dev/loop[0-9]+", self.loop):
            raise RuntimeError("Unexpected loop device")
        wait_loop_partitions(self.loop)
        self.run(["mkfs.ext4", "-F", "-m", "0", "-L", "system-root", self.loop + "p3"])
        self.run(["mkfs.vfat", "-F", "32", "-n", "UBUNTU_EFI", self.loop + "p2"])
        root = self.work / "target"
        self.mount([self.loop + "p3"], root)
        apt_cache = self.package_cache()
        self.bootstrap_system(root, apt_cache)
        self.mount([self.loop + "p2"], root / "boot/efi")
        self.api_mounts(root)
        cache_mount_count = len(self.mounts)
        if apt_cache:
            self.mount(["--bind", apt_cache], root / "var/cache/apt/archives")
        tools = root / "tmp/customizer"
        tools.mkdir()
        (tools / "build-root").write_text(self.id + "\n")
        packages = (REPO / 'components/server/packages.txt').read_text() + (self.target.directory / 'packages.txt').read_text()
        if self.config.get('legacy_packages'):
            packages = (REPO / 'config/packages.txt').read_text()
        (tools / 'packages.txt').write_text(packages)
        for stage in ('customize', 'check'):
            (tools / (stage + '.sh')).write_text(self.target.guest_script(stage))
        for name in ("server-info.sh", "server-update.sh", "check-v2raya.py", "proxy-shell.bash", "proxy-check.py", "ntfy_notify.py", "docker_multiarch.py"):
            shutil.copyfile(REPO / "scripts" / name, tools / name)
        self.install_ntfy_config(root)
        with self.progress("Downloading and verifying uv from GitHub"):
            self.uv(tools)
        with self.progress("Downloading and verifying latest stable v2rayA from GitHub"):
            self.v2raya(tools)
        guest_env = {"SYSTEM_MIRROR": self.config["system_mirror"], "SECURITY_MIRROR": self.config["security_mirror"],
                     "DOCKER_MIRROR": self.config["docker_mirror"], "PYTHON_INDEX": self.config["python_index"],
                     "TARGET_DISTRO": self.target.name, "TARGET_SUITE": self.target.suite,
                     "TARGET_USER": self.config["username"]}
        self.guest(root, self.target.guest_script('customize'), guest_env,
                   label=f"Installing packages and configuring {self.target.name}, Docker, Tailscale and v2rayA")
        self.run(["chroot", root, "chpasswd", "--encrypted"],
                 input=(self.config['username'] + ":" + self.password_hash + "\n").encode())
        root_uuid = self.output(["blkid", "-p", "-s", "UUID", "-o", "value", self.loop + "p3"])
        esp_uuid = self.output(["blkid", "-p", "-s", "UUID", "-o", "value", self.loop + "p2"])
        (root / "etc/fstab").write_text(f"UUID={root_uuid} / ext4 defaults 0 1\nUUID={esp_uuid} /boot/efi vfat umask=0077 0 2\n/swapfile none swap sw 0 0\n")
        self.guest(root, 'set -- ' + shlex.quote(self.loop) + ' ' + shlex.quote(root_uuid) + '\n' + self.target.guest_script('bootloader'),
                   label="Generating initramfs and installing BIOS / UEFI bootloaders")
        self.checks['grub_root_uuid'] = 'passed'
        self.guest(root, self.target.guest_script("check"), guest_env, label="Checking target packages and configuration")
        self.checks["target_packages_and_config"] = "passed"
        self.details["v2raya"]["core_version"] = self.output(["chroot", root, "v2raya_core", "version"])
        for name in ("packages.txt", "package-origins.txt"):
            shutil.copyfile(root / "var/lib/ubuntu-customizer" / name, self.release / name)
        self.clean_system(root, cache_mount_count)
        (root / "var/lib/ubuntu-customizer/build-id").write_text(self.id + "\n")
        self.details["kernel"] = sorted(p.name for p in (root / "boot").glob("vmlinuz-*"))
        self.unmount_to(0)
        self.run(["zerofree", self.loop + "p3"], label="Zeroing free space in target image")
        self.run(["losetup", "--detach", self.loop])
        self.loop = None
        self.note("Target packages/configuration checks passed; image unmounted")
        return raw

    def install_ntfy_config(self, root):
        if not self.ntfy_config:
            return
        directory = root / "etc/ubuntu-customizer"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / "ntfy.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(self.ntfy_config, stream)

    def uv(self, destination):
        version = self.config["uv_version"]
        if version == "latest":
            self.note("Querying latest uv release: api.github.com")
            req = urllib.request.Request("https://api.github.com/repos/astral-sh/uv/releases/latest", headers={"User-Agent": "ubuntu-customizer"})
            with urllib.request.urlopen(req, timeout=60) as response:
                version = json.load(response)["tag_name"]
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ValueError("Invalid uv release version")
        name = "uv-x86_64-unknown-linux-gnu.tar.gz"
        base = "https://github.com/astral-sh/uv/releases/download/" + version + "/"
        archive = self.work / name
        checksum = self.work / (name + ".sha256")
        self.note("Downloading uv checksum: github.com (redirects allowed)")
        download(base + name + ".sha256", checksum)
        sha = checksum.read_text().split()[0]
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise RuntimeError("Invalid uv checksum")
        self.note("Downloading and verifying uv archive: github.com (redirects allowed)")
        download(base + name, archive, sha)
        with tarfile.open(archive) as package:
            for binary in ("uv", "uvx"):
                with package.extractfile("uv-x86_64-unknown-linux-gnu/" + binary) as source, (destination / binary).open("wb") as target:
                    shutil.copyfileobj(source, target)
        self.details["uv"] = {"version": version, "archive_sha256": sha}

    def v2raya(self, destination):
        self.note("Querying latest stable v2rayA release: api.github.com")
        request = urllib.request.Request("https://api.github.com/repos/v2rayA/v2rayA/releases/latest",
                                         headers={"User-Agent": "ubuntu-customizer"})
        with urllib.request.urlopen(request, timeout=60) as response:
            release = json.load(response)
        tag = release.get("tag_name", "")
        if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) or release.get("draft") or release.get("prerelease"):
            raise RuntimeError("Unexpected v2rayA stable release metadata")
        name = f"installer_debian_x64_{tag[1:]}.deb"
        asset = next((item for item in release.get("assets", []) if item.get("name") == name), None)
        url = f"https://github.com/v2rayA/v2rayA/releases/download/{tag}/{name}"
        if not asset or asset.get("browser_download_url") != url:
            raise RuntimeError("Latest v2rayA release has no expected amd64 Debian package")
        checksum = asset.get("digest", "")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", checksum):
            raise RuntimeError("Missing SHA256 digest for v2rayA Debian package")
        sha = checksum.removeprefix("sha256:")
        self.note("Downloading and verifying v2rayA Debian package: github.com (redirects allowed)")
        download(url, destination / "v2raya.deb", sha)
        self.details["v2raya"] = {"version": tag[1:], "package": name, "url": url, "sha256": sha}
        (destination / "v2raya-release.json").write_text(json.dumps(self.details["v2raya"], indent=2) + "\n")

    def report(self, status, error=None):
        if not self.owns_release:
            return
        public = self.release / "reports"
        public.mkdir(exist_ok=True)
        result = {"build_id": self.id, "build": status, "manual_acceptance": "pending",
                  "executed_checks": self.checks,
                  "boot_test": "not_run", "installation_test": "not_run"}
        if error:
            # Exception text may contain command output; expose only its class.
            result["failure_type"] = type(error).__name__
        (public / "build-report.json").write_text(json.dumps(result, indent=2) + "\n")
        (public / "build.log").write_text("\n".join(self.logs) + "\n")
        # Raw logs may include package maintainer output. Keep a redacted local copy only.
        raw_log = self.work / "tools.log"
        if raw_log.exists():
            log_dir = self.release / "logs"
            log_dir.mkdir(exist_ok=True)
            text = raw_log.read_text(errors="replace")
            secrets = {self.secret, self.password_hash} | proxy_secrets(getattr(self, "proxy_env", {}))
            secrets |= set((getattr(self, "ntfy_config", None) or {}).values())
            for secret in sorted(secrets, key=len, reverse=True):
                if secret:
                    text = text.replace(secret, "<redacted>")
            (log_dir / "tools.log").write_text(text)
        self.stage.mkdir(exist_ok=True)
        for name, source in {"build-report.json": public / "build-report.json", "build.log": public / "build.log",
                             **{name: self.release / name for name in ("manifest.json", "packages.txt", "SHA256SUMS")}}.items():
            if source.exists():
                target = self.stage / name
                shutil.copyfile(source, target)
                target.chmod(0o644)
                if "SUDO_UID" in os.environ:
                    os.chown(target, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
        if "SUDO_UID" in os.environ:
            os.chown(self.stage, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))


def check_downloads(config):
    """Exercise real download paths without root, mounts, images or passwords."""
    try:
        values = configure_proxy()
        for line in proxy_summary(values):
            print(line, flush=True)
        with tempfile.TemporaryDirectory(prefix="ubuntu-customizer-downloads-") as folder:
            build = Build.__new__(Build)
            build.config = config
            build.work = Path(folder)
            build.logs = []
            build.details = {}
            tools = build.work / "tools"
            tools.mkdir()
            with build.progress("Downloading and verifying uv from GitHub"):
                build.uv(tools)
            with build.progress("Downloading and verifying latest stable v2rayA from GitHub"):
                build.v2raya(tools)
        print("GitHub downloads and checksums passed. Temporary downloads removed; no images, mounts or notifications created.")
        return 0
    except Exception as error:
        detail = http_error_summary(error) if isinstance(error, urllib.error.HTTPError) else type(error).__name__
        print("Download check failed: " + detail, file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Host settings; legacy Ubuntu config is accepted")
    parser.add_argument("--profile", type=Path, help="Target profile (default: profiles/ubuntu-server.json)")
    parser.add_argument("--build-id", default=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--reports", type=Path, default=REPO / "reports")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read-only host checks; no password, sudo or build needed")
    mode.add_argument("--check-downloads", action="store_true", help="Check GitHub downloads using build proxy settings; no image build")
    args = parser.parse_args()
    if args.config is None:
        args.config = REPO / ('config/build.json' if args.profile is None else 'config/host.json')
    if args.check_downloads:
        try:
            config, _ = load_config(args.config, args.profile)
        except (OSError, ValueError) as error:
            print('Download configuration failed: ' + str(error), file=sys.stderr)
            return 1
        return check_downloads(config)
    if args.check:
        try:
            result = check_host(load_config(args.config, args.profile)[0])
            print("Host checks passed. No directories, mounts or images were created.")
            print(json.dumps(result, indent=2))
            print("Build-time mount/loop access and network downloads are checked when building.")
            return 0
        except Exception as error:
            print("Host check failed: " + str(error), file=sys.stderr)
            return 1
    os.umask(0o022)
    def cancelled(signum, frame):
        raise InterruptedError("Build interrupted")
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    build = None
    try:
        build = Build(args)
        build.preflight()
        raw = build.system_image()
        build.package(raw)
        build.report("passed")
        print("ISO saved locally: " + str(build.release / build.target.iso_name))
        return 0
    except Exception as error:
        reason = ": " + str(error) if isinstance(error, (ValueError, RuntimeError, FileExistsError)) else ""
        if isinstance(error, urllib.error.HTTPError):
            reason = ": " + http_error_summary(error)
        if isinstance(error, sp.CalledProcessError):
            # Never print str(error): it includes all arguments and possibly private input.
            tool = Path(str(error.cmd[0])).name if isinstance(error.cmd, (list, tuple)) and error.cmd else "subprocess"
            reason = f": {tool}; exit code {error.returncode}"
        log_path = str(build.work / "tools.log") if build is not None else "local work/tools.log"
        print("Build failed: " + type(error).__name__ + reason + "; inspect " + log_path, file=sys.stderr)
        if build is not None:
            build.note("Build failed: " + type(error).__name__ + (reason if isinstance(error, urllib.error.HTTPError) else ""))
            build.report("failed", error)
        return 1
    finally:
        if build is not None:
            try:
                build.unmount_to(0)
                if build.loop:
                    build.run(["losetup", "--detach", build.loop])
            except Exception:
                print("Cleanup incomplete; inspect mounts and loop devices before removing work directory", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

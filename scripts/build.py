#!/usr/bin/env python3
"""Build with private mount, PID, UTS and IPC namespaces using file-backed disks."""
import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import gzip
import json
import lzma
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess as sp
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import urllib.error
import urllib.parse

if __package__:
    from .build_env import configure_proxy, password_input, proxy_secrets, ntfy_settings, NTFY_KEYS
else:
    # Direct execution and importlib-based safety tests use different search paths.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from build_env import configure_proxy, password_input, proxy_secrets, ntfy_settings, NTFY_KEYS

REPO = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3
PROGRESS_INTERVAL = 30
ALPINE_FALLBACK_MIRRORS = ["https://mirrors.tuna.tsinghua.edu.cn/alpine"]


def alpine_mirrors(config):
    fallbacks = config.get("alpine_fallback_mirrors", ALPINE_FALLBACK_MIRRORS)
    if not isinstance(fallbacks, list):
        raise ValueError("alpine_fallback_mirrors must be a list of HTTPS URLs")
    mirrors = []
    for mirror in [config["alpine_mirror"], *fallbacks]:
        if not isinstance(mirror, str) or not mirror.startswith("https://") or any(c.isspace() for c in mirror):
            raise ValueError("HTTPS URL required for Alpine mirror")
        mirror = mirror.rstrip("/")
        if mirror not in mirrors:
            mirrors.append(mirror)
    return mirrors


def check_build_isolation():
    for namespace in ("mnt", "pid", "uts", "ipc"):
        original = os.environ.get("UBUNTU_CUSTOMIZER_HOST_" + namespace.upper())
        if not original or os.readlink("/proc/self/ns/" + namespace) == original:
            raise RuntimeError("Private " + namespace + " namespace required; use scripts/build.sh")
    if os.getpid() != 1 or not Path("/proc/1").exists():
        raise RuntimeError("Private PID namespace and matching procfs required; use scripts/build.sh")


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 ** 2), b""):
            value.update(chunk)
    return value.hexdigest()


def wait_loop_partitions(loop, timeout=10):
    """Wait for both image partitions before running any formatter."""
    deadline = time.monotonic() + timeout
    while True:
        ready = True
        for number in (2, 3):
            try:
                ready = stat.S_ISBLK(Path(loop + f"p{number}").stat().st_mode) and ready
            except FileNotFoundError:
                ready = False
        if ready:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Loop EFI/root partition devices did not become ready: {loop}p2, {loop}p3")
        time.sleep(0.1)


def download(url, destination, expected=None):
    request = urllib.request.Request(url, headers={"User-Agent": "ubuntu-customizer"})
    temporary = destination.with_suffix(destination.suffix + ".partial")
    with urllib.request.urlopen(request, timeout=120) as source, temporary.open("wb") as target:
        shutil.copyfileobj(source, target)
    if expected and digest(temporary) != expected:
        temporary.unlink()
        raise RuntimeError("Downloaded file SHA256 mismatch: " + destination.name)
    temporary.replace(destination)


def http_error_summary(error):
    # Never use str(HTTPError): the reason, URL or headers may expose proxy
    # credentials or signed redirect query strings. Only report status + host.
    try:
        host = urllib.parse.urlsplit(error.url).hostname or "remote server"
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            host = "remote server"
    except ValueError:
        host = "remote server"
    message = f"HTTP {error.code} from {host}"
    if error.code in (403, 429) and error.headers and error.headers.get("X-RateLimit-Remaining") == "0":
        message += "; GitHub API rate limit exhausted; wait for reset or change proxy egress"
    elif error.code == 407:
        message += "; proxy authentication required"
    elif error.code == 403:
        message += "; request forbidden; check GitHub limits and proxy policy"
    elif error.code == 404:
        message += "; resource not found; check the release/asset and proxy response"
    elif error.code == 429:
        message += "; too many requests; retry later"
    elif error.code in (502, 503, 504):
        message += "; gateway or upstream unavailable; check proxy/connectivity and retry"
    return message


def proxy_summary(values):
    lines = ["Build proxy: HTTP=" + ("configured" if values.get("http_proxy") else "unset")
             + "; HTTPS=" + ("configured" if values.get("https_proxy") else "unset")]
    for host in ("api.github.com", "github.com", "release-assets.githubusercontent.com"):
        bypass = urllib.request.proxy_bypass_environment(host, {"no": values.get("no_proxy", "")})
        route = "direct (NO_PROXY match)" if bypass else "proxy" if values.get("https_proxy") else "direct (HTTPS proxy unset)"
        lines.append(f"Configured route for {host}: {route}")
    return lines


def check_host(config):
    """Read-only checks, also usable before credentials and build directories exist."""
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise RuntimeError("A Linux x86_64 build machine is required")
    tools = ("debootstrap", "losetup", "sgdisk", "mkfs.ext4", "zerofree", "mkfs.vfat", "mksquashfs",
             "grub-mkrescue", "xorriso", "zstd", "cpio", "depmod", "openssl", "unshare")
    missing = [tool for tool in tools if not shutil.which(tool)]
    if missing:
        raise RuntimeError("Missing tools: " + ", ".join(missing) + "; run bash build.sh setup")
    try:
        import yaml  # noqa: F401; required later to read official Alpine metadata.
    except ImportError as error:
        raise RuntimeError("python3-yaml is missing; run bash build.sh setup") from error
    for platform in ("i386-pc", "x86_64-efi"):
        if not Path("/usr/lib/grub", platform, "modinfo.sh").is_file():
            raise RuntimeError("Install GRUB modules for " + platform)
    available = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))
    if available < 4 * GIB:
        raise RuntimeError("At least 4 GiB currently available build memory required")
    paths = []
    minimum_free = config["minimum_build_free_gib"]
    if type(minimum_free) is not int or not 40 <= minimum_free <= 1024:
        raise ValueError("Invalid minimum_build_free_gib")
    for key in ("work_dir", "release_dir"):
        path = Path(config[key])
        if not path.is_absolute():
            raise ValueError("Absolute persistent paths required")
        path = path.resolve()
        if path == Path("/") or path == REPO or REPO in path.parents:
            raise ValueError("Use persistent work/release directories outside the checkout")
        paths.append(path)
        existing = path
        while not existing.exists():
            existing = existing.parent
        if not existing.is_dir():
            raise ValueError("Build path has a non-directory ancestor")
        if shutil.disk_usage(existing).free < minimum_free * GIB:
            raise RuntimeError("Insufficient free build space at " + str(path))
    if paths[0] == paths[1] or paths[0] in paths[1].parents or paths[1] in paths[0].parents:
        raise ValueError("Work and release directories must be separate")
    return {"available_memory_gib": round(available / GIB, 1),
            "work_dir": str(paths[0]), "release_dir": str(paths[1])}


class Build:
    def __init__(self, args):
        self.config = json.loads(args.config.read_text())
        self.id = args.build_id
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,90}", self.id):
            raise ValueError("Invalid build id")
        if self.config["ubuntu_suite"] != "noble":
            raise ValueError("Only Ubuntu 24.04 noble is supported")
        for key, minimum, maximum in (("template_gib", 16, 128), ("compression_threads", 1, 16), ("minimum_build_free_gib", 40, 1024)):
            value = self.config[key]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("Invalid resource setting: " + key)
        for key in ("ubuntu_mirror", "docker_mirror", "python_index", "alpine_mirror"):
            if not self.config[key].startswith("https://") or any(c.isspace() for c in self.config[key]):
                raise ValueError("HTTPS URL required: " + key)
        alpine_mirrors(self.config)
        if not Path(self.config["work_dir"]).is_absolute() or not Path(self.config["release_dir"]).is_absolute():
            raise ValueError("Absolute persistent paths required")
        self.work_base = Path(self.config["work_dir"]).resolve()
        self.release_base = Path(self.config["release_dir"]).resolve()
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
        self.details = {"build_id": self.id, "architecture": "amd64", "ubuntu": "24.04 LTS", "manual_acceptance": "pending"}
        self.proxy_env = configure_proxy()
        self.ntfy_config = ntfy_settings(os.environ)
        for key in NTFY_KEYS:
            os.environ.pop(key, None)
        self.secret = password_input(os.environ, REPO / "config/local.env")
        self.password_hash = sp.check_output(["openssl", "passwd", "-6", "-stdin"], input=self.secret.encode()).decode().strip()
        os.environ.pop("UBUNTU_INITIAL_PASSWORD", None)

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

    def ubuntu(self):
        self.note("Building Ubuntu 24.04 disk image using signed domestic APT repositories")
        raw = self.work / "ubuntu.raw"
        with raw.open("xb") as stream:
            stream.truncate(int(self.config["template_gib"]) * GIB)
        self.run(["sgdisk", "--clear", "--new=1:2048:+2M", "--typecode=1:ef02", "--new=2:0:+512M", "--typecode=2:ef00", "--new=3:0:0", "--typecode=3:8300", raw])
        self.loop = self.output(["losetup", "--find", "--show", "--partscan", raw])
        if not re.fullmatch(r"/dev/loop[0-9]+", self.loop):
            raise RuntimeError("Unexpected loop device")
        wait_loop_partitions(self.loop)
        self.run(["mkfs.ext4", "-F", "-m", "0", "-L", "ubuntu-root", self.loop + "p3"])
        self.run(["mkfs.vfat", "-F", "32", "-n", "UBUNTU_EFI", self.loop + "p2"])
        root = self.work / "ubuntu"
        self.mount([self.loop + "p3"], root)
        # Jammy's debootstrap generic Ubuntu script also supports noble on a 22.04 host.
        script = "/usr/share/debootstrap/scripts/gutsy"
        self.run(["debootstrap", "--arch=amd64", "--merged-usr", "--variant=minbase", "--keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg", "noble", root, self.config["ubuntu_mirror"], script], label="Installing Ubuntu base system (debootstrap)")
        self.mount([self.loop + "p2"], root / "boot/efi")
        self.api_mounts(root)
        tools = root / "tmp/customizer"
        tools.mkdir()
        (tools / "build-root").write_text(self.id + "\n")
        shutil.copyfile(REPO / "config/packages.txt", tools / "packages.txt")
        for name in ("server-info.sh", "server-update.sh", "check-v2raya.py", "proxy-shell.bash", "proxy-check.py", "ntfy_notify.py"):
            shutil.copyfile(REPO / "scripts" / name, tools / name)
        self.install_ntfy_config(root)
        with self.progress("Downloading and verifying uv from GitHub"):
            self.uv(tools)
        with self.progress("Downloading and verifying latest stable v2rayA from GitHub"):
            self.v2raya(tools)
        self.guest(root, (REPO / "scripts/customize.sh").read_text(), {
            "UBUNTU_MIRROR": self.config["ubuntu_mirror"],
            "DOCKER_MIRROR": self.config["docker_mirror"], "PYTHON_INDEX": self.config["python_index"]},
            label="Installing packages and configuring Ubuntu, Docker, Tailscale and v2rayA")
        self.run(["chroot", root, "chpasswd", "--encrypted"], input=("ubuntu:" + self.password_hash + "\n").encode())
        root_uuid = self.output(["blkid", "-p", "-s", "UUID", "-o", "value", self.loop + "p3"])
        esp_uuid = self.output(["blkid", "-p", "-s", "UUID", "-o", "value", self.loop + "p2"])
        (root / "etc/fstab").write_text(f"UUID={root_uuid} / ext4 defaults 0 1\nUUID={esp_uuid} /boot/efi vfat umask=0077 0 2\n/swapfile none swap sw 0 0\n")
        self.guest(root, "update-initramfs -u -k all\ngrub-install --target=i386-pc --recheck " + self.loop + "\ngrub-install --target=x86_64-efi --efi-directory=/boot/efi --removable --no-nvram --no-uefi-secure-boot\nupdate-grub\n", label="Generating initramfs and installing BIOS / UEFI bootloaders")
        self.guest(root, (REPO / "scripts/check-guest.sh").read_text(), label="Checking Ubuntu packages and configuration")
        self.checks["ubuntu_packages_and_config"] = "passed"
        self.details["v2raya"]["core_version"] = self.output(["chroot", root, "v2raya_core", "version"])
        for name in ("packages.txt", "package-origins.txt"):
            shutil.copyfile(root / "var/lib/ubuntu-customizer" / name, self.release / name)
        self.guest(root, """apt-get clean
rm -rf /tmp/customizer /root/.cache /root/.ssh /home/ubuntu/.cache /home/ubuntu/.ssh /var/lib/tailscale /var/lib/docker /var/lib/containerd
rm -f /etc/ssh/ssh_host_* /var/lib/systemd/random-seed /root/.bash_history /home/ubuntu/.bash_history
find /var/log -type f -exec truncate -s 0 {} +
find /var/lib/apt/lists -type f ! -name lock -delete
rm -f /etc/machine-id /var/lib/dbus/machine-id
: > /etc/machine-id
ln -s /etc/machine-id /var/lib/dbus/machine-id
rm -f /etc/resolv.conf
ln -s /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
""")
        (root / "var/lib/ubuntu-customizer/build-id").write_text(self.id + "\n")
        self.details["kernel"] = sorted(p.name for p in (root / "boot").glob("vmlinuz-*"))
        self.unmount_to(0)
        self.run(["zerofree", self.loop + "p3"], label="Zeroing free space in Ubuntu image")
        self.run(["losetup", "--detach", self.loop])
        self.loop = None
        self.note("Ubuntu packages/configuration checks passed; image unmounted")
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

    def install_alpine_packages(self, root, branch):
        if not re.fullmatch(r"v3\.[0-9]+", branch):
            raise ValueError("Invalid Alpine branch")
        mirrors = alpine_mirrors(self.config)
        packages = ["alpine-base", "linux-firmware-none", "linux-lts", "busybox-static",
                    "python3", "bash", "util-linux", "lsblk", "findmnt", "wipefs", "parted", "sgdisk",
                    "e2fsprogs", "e2fsprogs-extra", "dosfstools", "zstd", "coreutils", "kmod"]
        for index, mirror in enumerate(mirrors):
            (root / "etc/apk/repositories").write_text(
                f"{mirror}/{branch}/main\n{mirror}/{branch}/community\n")
            for attempt in (1, 2):
                try:
                    # --no-cache fetches fresh indexes on each attempt. Keep default
                    # signature verification and the branch selected from metadata.
                    self.run(["chroot", root, "apk", "add", "--no-cache", *packages],
                             label=f"Installing Alpine Live packages (APK; mirror {index + 1}/{len(mirrors)}, attempt {attempt}/2)")
                    return mirror
                except sp.CalledProcessError:
                    if attempt == 2 and index == len(mirrors) - 1:
                        raise
                    self.note("APK installation failed; " + (
                        "retrying with fresh indexes in 5s" if attempt == 1 else
                        "switching to next configured Alpine mirror in 5s") + "; details in tools.log")
                    time.sleep(5)

    def alpine(self, iso):
        import yaml
        self.note("Building Alpine Live root and RAM boot environment")
        mirror = self.config["alpine_mirror"].rstrip("/")
        release = self.config["alpine_release"]
        if not re.fullmatch(r"latest-stable|v3\.[0-9]+", release):
            raise ValueError("Invalid Alpine branch")
        metadata = self.work / "alpine-releases.yaml"
        with self.progress("Downloading Alpine release metadata"):
            download(f"{mirror}/{release}/releases/x86_64/latest-releases.yaml", metadata)
        entry = next(item for item in yaml.safe_load(metadata.read_text()) if item.get("flavor") == "alpine-minirootfs")
        branch = entry["branch"]
        name = entry["file"]
        if not re.fullmatch(r"v3\.[0-9]+", branch) or Path(name).name != name:
            raise RuntimeError("Unexpected Alpine release metadata")
        archive = self.work / name
        with self.progress("Downloading and verifying Alpine minirootfs"):
            download(f"{mirror}/{branch}/releases/x86_64/{name}", archive, entry["sha256"])
        root = self.work / "alpine"
        root.mkdir()
        self.run(["tar", "-xzf", archive, "-C", root])
        self.api_mounts(root)
        # Alpine packages the scripted GPT tool separately from interactive gdisk.
        apk_mirror = self.install_alpine_packages(root, branch)
        library = root / "usr/local/lib/ubuntu-customizer"
        library.mkdir(parents=True)
        for name in ("installer.py", "finalize.sh", "partition_devices.py"):
            shutil.copyfile(REPO / "installer" / name, library / name)
        self.run(["chroot", root, "/usr/bin/env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                  "python3", "/usr/local/lib/ubuntu-customizer/installer.py", "--check-tools"],
                 label="Checking Alpine Live installer tools before ISO packaging")
        self.checks["alpine_installer_tools"] = "passed"
        console = root / "usr/local/sbin/installer-console"
        console.parent.mkdir(parents=True, exist_ok=True)
        console.write_text("#!/bin/sh\nexport PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\nif python3 /usr/local/lib/ubuntu-customizer/installer.py; then\n  echo 'Live shell: follow the completion steps above; run reboot when ready.'\nelse\n  echo 'Installer stopped. Manual recovery shell; reboot only after unmounting disks.'\nfi\nexec /bin/sh\n")
        console.chmod(0o755)
        (root / "etc/inittab").write_text("::sysinit:/sbin/openrc sysinit\n::wait:/sbin/openrc boot\n::wait:/sbin/openrc default\ntty1::respawn:/usr/local/sbin/installer-console\ntty2::respawn:/bin/sh\n::ctrlaltdel:/sbin/reboot\n::shutdown:/sbin/openrc shutdown\n")
        self.run(["chroot", root, "rc-update", "add", "devfs", "sysinit"])
        self.run(["chroot", root, "rc-update", "add", "mdev", "sysinit"])
        self.run(["chroot", root, "rc-update", "add", "hwdrivers", "sysinit"])
        self.details["alpine"] = {"version": entry["version"], "branch": branch,
                                  "minirootfs_sha256": entry["sha256"], "apk_mirror": apk_mirror}
        self.details["alpine"]["kernels"] = sorted(path.name for path in (root / "lib/modules").iterdir())
        self.details["alpine"]["packages"] = self.output(["chroot", root, "apk", "info", "-v"]).splitlines()
        self.unmount_to(0)
        boot = iso / "boot"
        boot.mkdir(parents=True)
        shutil.copyfile(root / "boot/vmlinuz-lts", boot / "vmlinuz")
        initrd = self.work / "initrd"
        (initrd / "bin").mkdir(parents=True)
        shutil.copyfile(root / "bin/busybox.static", initrd / "bin/busybox")
        (initrd / "bin/busybox").chmod(0o755)
        # A static busybox plus every installed storage module avoids host-specific initrd generation.
        for applet in ("sh", "mount", "umount", "mkdir", "chmod", "cp", "sleep", "modprobe", "mdev", "switch_root", "blkid", "cat"):
            (initrd / "bin" / applet).symlink_to("busybox")
        shutil.copytree(root / "lib/modules", initrd / "lib/modules", symlinks=True)
        # Busybox's modprobe need not support the kernel package's compression format.
        for path in (initrd / "lib/modules").rglob("*.ko.*"):
            destination = path.with_suffix("")
            if path.suffix in (".gz", ".xz"):
                opener = gzip.open if path.suffix == ".gz" else lzma.open
                with opener(path, "rb") as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
            elif path.suffix == ".zst":
                with destination.open("wb") as target:
                    sp.run(["zstd", "--decompress", "--stdout", str(path)], stdout=target, check=True)
            else:
                raise RuntimeError("Unsupported kernel module compression")
            path.unlink()
        for kernel in (initrd / "lib/modules").iterdir():
            self.run(["depmod", "--basedir", initrd, kernel.name])
        (initrd / "sbin").mkdir()
        (initrd / "sbin/modprobe").symlink_to("../bin/busybox")
        shutil.copyfile(REPO / "installer/live-init.sh", initrd / "init")
        (initrd / "init").chmod(0o755)
        for path in ("dev", "proc", "sys", "media/installer", "lower", "newroot"):
            (initrd / path).mkdir(parents=True, exist_ok=True)
        self.run(["bash", "-o", "pipefail", "-c", 'find . -print0 | cpio --null -o --format=newc | gzip -1 > "$1"', "initrd", str(boot / "initramfs")], cwd=initrd)
        self.run(["mksquashfs", root, iso / "live.squashfs", "-noappend", "-comp", "zstd", "-processors", str(self.config["compression_threads"]), "-e", "dev", "proc", "sys", "run", "tmp", "boot"])

    def package(self, raw):
        iso = self.work / "iso"
        payload = iso / "payload"
        payload.mkdir(parents=True)
        self.note("Hashing and compressing the complete Ubuntu disk image")
        with self.progress("Calculating Ubuntu disk image SHA256"):
            raw_hash = digest(raw)
        self.details.update({"format": 1, "sector_size": 512, "root_partition": 3, "esp_partition": 2,
                             "raw_bytes": raw.stat().st_size, "raw_sha256": raw_hash})
        compressed = payload / "ubuntu.img.zst"
        self.run(["zstd", "-T" + str(self.config["compression_threads"]), "-8", raw, "-o", compressed], label="Compressing Ubuntu disk image (zstd)")
        self.run(["zstd", "--test", compressed], label="Verifying compressed disk image")
        self.checks["compressed_stream"] = "passed"
        with self.progress("Calculating compressed image SHA256"):
            self.details["compressed_sha256"] = digest(compressed)
        self.alpine(iso)
        self.details["sources"] = {key: self.config[key] for key in ("ubuntu_mirror", "docker_mirror", "python_index")}
        self.details["sources"]["tailscale"] = "https://pkgs.tailscale.com/stable/ubuntu"
        self.details["git_commit"] = None
        if shutil.which("git"):
            git = sp.run(["git", "-c", "safe.directory=" + str(REPO), "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True)
            if git.returncode == 0:
                self.details["git_commit"] = git.stdout.strip()
        self.details["input_sha256"] = {str(path.relative_to(REPO)): digest(path)
                                         for directory in ("scripts", "installer", "config")
                                         for path in sorted((REPO / directory).glob("*"))
                                         if path.is_file() and path.suffix in (".py", ".sh", ".bash", ".json", ".txt")}
        self.details["input_sha256"]["build.sh"] = digest(REPO / "build.sh")
        manifest = json.dumps(self.details, indent=2) + "\n"
        (payload / "manifest.json").write_text(manifest)
        (payload / "SHA256SUMS").write_text(self.details["compressed_sha256"] + "  ubuntu.img.zst\n")
        (iso / "UBUNTU_CUSTOMIZER_MEDIA").write_text(self.id + "\n")
        (iso / "boot/grub").mkdir(parents=True)
        (iso / "boot/grub/grub.cfg").write_text('set timeout=3\nset default=0\nmenuentry "Install customized Ubuntu Server (erases selected disk after confirmation)" {\n linux /boot/vmlinuz console=tty0\n initrd /boot/initramfs\n}\n')
        shutil.copyfile(REPO / "LICENSE", iso / "LICENSE")
        # Package copyright notices accompany the installed system; Alpine notices remain in live root.
        output = self.release / "ubuntu-custom-server.iso.partial"
        self.run(["grub-mkrescue", "-o", output, iso, "--", "-volid", "UCINSTALL", "-compliance", "iso_9660_level=3"], label="Packaging BIOS / UEFI installer ISO")
        boot_report = sp.check_output(["xorriso", "-indev", str(output), "-report_el_torito", "plain"], stderr=sp.STDOUT, text=True)
        # xorriso writes report text to stdout; both firmware platform records are required.
        if not re.search(r"BIOS", boot_report) or not re.search(r"UEFI", boot_report):
            raise RuntimeError("ISO lacks dual firmware El Torito records")
        self.checks["bios_uefi_boot_catalog"] = "passed"
        self.run(["xorriso", "-indev", output, "-check_media", "--"], label="Checking ISO media integrity")
        self.checks["iso_read_check"] = "passed"
        with self.progress("Calculating ISO SHA256"):
            iso_hash = digest(output)
        output.replace(self.release / "ubuntu-custom-server.iso")
        (self.release / "manifest.json").write_text(manifest)
        (self.release / "SHA256SUMS").write_text(iso_hash + "  ubuntu-custom-server.iso\n")
        os.sync()
        self.note("Hybrid ISO created; package/configuration and image integrity checks passed")

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
    parser.add_argument("--config", type=Path, default=REPO / "config/build.json")
    parser.add_argument("--build-id", default=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--reports", type=Path, default=REPO / "reports")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read-only host checks; no password, sudo or build needed")
    mode.add_argument("--check-downloads", action="store_true", help="Check GitHub downloads using build proxy settings; no image build")
    args = parser.parse_args()
    if args.check_downloads:
        return check_downloads(json.loads(args.config.read_text()))
    if args.check:
        try:
            result = check_host(json.loads(args.config.read_text()))
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
        raw = build.ubuntu()
        build.package(raw)
        build.report("passed")
        print("ISO saved locally: " + str(build.release / "ubuntu-custom-server.iso"))
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

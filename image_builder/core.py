"""Build isolation, host validation, paths and checked downloads."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from image_builder.distros import target_for

REPO = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3
ALPINE_FALLBACK_MIRRORS = ['https://mirrors.tuna.tsinghua.edu.cn/alpine']


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


def package_cache_directory(config):
    value = config.get("cache_dir", str(Path(config["work_dir"]).parent / "cache"))
    if value is None:
        return None
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("cache_dir must be an absolute directory path, or null to disable caching")
    cache = Path(value).resolve()
    for other in (Path("/"), REPO, Path(config["work_dir"]).resolve(), Path(config["release_dir"]).resolve()):
        if cache == other or (other != Path("/") and (cache in other.parents or other in cache.parents)):
            raise ValueError("cache_dir must be separate from checkout, work and release directories")
    return cache


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
    target = target_for(config.get('distro', 'ubuntu'), config.get('version', '24.04'))
    if not Path(target.keyring).is_file() or not Path(target.bootstrap_script).is_file():
        raise RuntimeError('Target signing keyring/bootstrap script missing; run bash build.sh setup')
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
    package_cache_directory(config)
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

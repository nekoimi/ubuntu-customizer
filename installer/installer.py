#!/usr/bin/env python3
"""Offline whole-disk installer. Discovery and confirmation precede every write."""
import argparse
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import stat
import subprocess as sp
import sys
import tempfile
import threading
import time
import traceback

MEDIA = Path("/media/installer")
PAYLOAD = MEDIA / "payload"
SESSION = None
# Includes tools used by finalize.sh outside the Ubuntu chroot, where missing
# Alpine subpackages would otherwise be discovered only AFTER overwriting a disk.
LIVE_TOOLS = ("bash", "python3", "zstd", "lsblk", "findmnt", "wipefs", "sgdisk", "parted", "partprobe",
              "e2fsck", "resize2fs", "tune2fs", "fatlabel", "blkid", "mount", "umount",
              "chroot", "awk", "od", "tr", "mkdir", "install", "dd", "chmod", "date", "sync")


class Refusal(RuntimeError):
    pass


class InstallationReport:
    def __init__(self, log_root=Path("/var/log/ubuntu-customizer")):
        log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("install-%Y%m%dT%H%M%SZ-"), dir=log_root))
        self.log_path = self.directory / "installation.log"
        self.summary_path = self.directory / "error.txt"
        self.stage_path = self.directory / "finalize-stage.txt"
        self.command_path = self.directory / "finalize-error.txt"
        self.stage = "Starting installer"
        self.disk = None
        self.build_id = "unknown"
        self.writes_started = False
        self.console_isatty = os.isatty(1)
        self.recent = deque(maxlen=12)
        self.capture_error = None

    @contextmanager
    def capture(self):
        sys.stdout.flush()
        sys.stderr.flush()
        terminal = os.dup(1)
        original_error = os.dup(2)
        reader, writer = os.pipe()
        with self.log_path.open("xb", buffering=0) as log:
            os.chmod(self.log_path, 0o600)

            def copy_output():
                with os.fdopen(reader, "rb", buffering=0) as source:
                    while chunk := source.read(8192):
                        try:
                            log.write(chunk)
                        except OSError as error:
                            self.capture_error = error
                        self.recent.append(chunk.decode("utf-8", errors="replace"))
                        view = memoryview(chunk)
                        try:
                            while view:
                                view = view[os.write(terminal, view):]
                        except OSError:
                            pass

            copier = threading.Thread(target=copy_output, daemon=True)
            copier.start()
            try:
                os.dup2(writer, 1)
                os.dup2(writer, 2)
                os.close(writer)
                yield
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                os.dup2(terminal, 1)
                os.dup2(original_error, 2)
                copier.join()
                os.close(terminal)
                os.close(original_error)

    def diagnostics(self):
        commands = [["lsblk", "--paths", "--output", "NAME,TYPE,SIZE,PARTN,FSTYPE,UUID,MOUNTPOINTS"],
                    ["cat", "/proc/partitions"], ["ls", "-l", "/sys/class/block"],
                    ["findmnt"], ["dmesg"]]
        if self.disk:
            commands.insert(1, ["parted", "--script", self.disk, "unit", "s", "print"])
            commands.insert(2, ["python3", str(Path(__file__).with_name("partition_devices.py")), "--diagnose", self.disk])
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write("\n=== Automatic read-only diagnostics ===\n")
            for command in commands:
                log.write("\n$ " + " ".join(command) + "\n")
                try:
                    result = sp.run(command, stdout=sp.PIPE, stderr=sp.STDOUT, text=True,
                                    errors="replace", timeout=5, check=False)
                    log.write(result.stdout[-32768:] + f"\nExit: {result.returncode}\n")
                except (OSError, sp.TimeoutExpired) as error:
                    log.write(f"Diagnostic unavailable: {error}\n")
            if self.disk:
                # Inspect the actual children reported by lsblk; do not guess
                # sda3 versus nvme0n1p3 or probe unrelated disks.
                try:
                    tree = sp.run(["lsblk", "--json", "--tree", "--paths", "--output", "PATH,TYPE", self.disk],
                                  capture_output=True, text=True, timeout=5, check=True)
                    partitions = [node for node in flatten(json.loads(tree.stdout)["blockdevices"]) if node.get("type") == "part"]
                    for node in partitions[:8]:
                        for tool in (["blkid", "-p"], ["wipefs", "--no-act"]):
                            command = [*tool, "--", node["path"]]
                            log.write("\n$ " + " ".join(command) + "\n")
                            try:
                                result = sp.run(command, stdout=sp.PIPE, stderr=sp.STDOUT, text=True,
                                                errors="replace", timeout=3, check=False)
                                log.write(result.stdout[-8192:] + f"\nExit: {result.returncode}\n")
                            except (OSError, sp.TimeoutExpired) as error:
                                log.write(f"Probe unavailable: {error}\n")
                except (OSError, sp.SubprocessError, ValueError, KeyError) as error:
                    log.write(f"Partition diagnostics unavailable: {error}\n")

    def failure(self, error):
        stage = self.stage_path.read_text().strip() if self.stage_path.exists() else self.stage
        command = self.command_path.read_text().strip() if self.command_path.exists() else ""
        if isinstance(error, sp.CalledProcessError) and not command:
            command = f"exit {error.returncode}: {error.cmd}"
        details = [f"Stage: {stage}", f"Build: {self.build_id}", f"Target: {self.disk or 'not selected'}",
                   f"Target modifications started: {self.writes_started}",
                   f"Exception: {type(error).__name__}: {error}", f"Command: {command or 'see Python traceback'}"]
        recent = "".join(self.recent).replace("\r", "\n").splitlines()[-10:]
        self.summary_path.write_text("\n".join(details + ["", "Recent output:", *recent]) + "\n", encoding="utf-8")
        os.chmod(self.summary_path, 0o600)
        print("\nInstallation stopped. Collecting read-only diagnostics (up to 5s per command)...", flush=True)
        try:
            self.diagnostics()
        except (OSError, sp.SubprocessError, ValueError) as diagnostic_error:
            print(f"Some automatic diagnostics were unavailable: {diagnostic_error}", file=sys.stderr)
        interaction_panel("[ERROR] INSTALLATION STOPPED", [
            *details, "", "RECENT OUTPUT", *recent, "",
            f"Full log: {self.log_path}", f"Summary : {self.summary_path}",
            "Read the full log: less " + str(self.log_path),
            "Logs are in Live RAM. Copy this directory before rebooting.",
            "Installation is incomplete. No automatic retry or reboot.",
        ], color="31")


def installation_stage(stage, *, disk=None, build_id=None, writes_started=False):
    if SESSION is not None:
        SESSION.stage = stage
        if disk:
            SESSION.disk = disk
        if build_id:
            SESSION.build_id = build_id
        if writes_started:
            SESSION.writes_started = True


def check_live_tools():
    missing = [tool for tool in LIVE_TOOLS if not shutil.which(tool)]
    if missing:
        raise Refusal("Missing Alpine Live tools: " + ", ".join(missing) + "; rebuild the installer ISO. No target disk has been written.")
    if not Path(__file__).with_name("partition_devices.py").is_file():
        raise Refusal("Missing partition discovery helper; rebuild the installer ISO. No target disk has been written.")
    if not Path(__file__).with_name('bootloader.sh').is_file():
        raise Refusal('Missing target boot adapter; rebuild the installer ISO. No target disk has been written.')


def output(*args):
    result = sp.run(args, stdout=sp.PIPE, stderr=sp.PIPE, text=True)
    if result.returncode:
        print(result.stdout, end="", file=sys.stderr)
        print(result.stderr, end="", file=sys.stderr)
        result.check_returncode()
    return result.stdout.strip()


def sha256_file(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 ** 2), b""):
            value.update(chunk)
    return value.hexdigest()


def validate_manifest(manifest):
    if type(manifest.get("format")) is not int or manifest["format"] not in (1, 2) or manifest.get("architecture") != "amd64":
        raise Refusal("Unsupported payload format or architecture")
    if manifest['format'] == 2:
        if (manifest.get('distro'), manifest.get('version')) not in (('ubuntu', '24.04'), ('debian', '13')):
            raise Refusal('Unsupported target system')
        if manifest.get('payload') != 'system.img.zst' or manifest.get('root_filesystem') != 'ext4' or manifest.get('finalizer') != 'debian-family-v1':
            raise Refusal('Unsupported payload or finalizer')
        user = manifest.get('username')
        if not isinstance(user, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', user) or user in ('root', 'daemon', 'nobody'):
            raise Refusal('Invalid target username')
    if manifest.get("sector_size") != 512 or manifest.get("root_partition") != 3 or manifest.get("esp_partition") != 2:
        raise Refusal("Unsupported disk layout")
    size = manifest.get("raw_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size % 512:
        raise Refusal("Invalid raw image size")
    for key in ("raw_sha256", "compressed_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", manifest.get(key, "")):
            raise Refusal("Invalid payload hash")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,90}", manifest.get("build_id", "")):
        raise Refusal("Invalid build id")


def flatten(nodes):
    for node in nodes:
        yield node
        yield from flatten(node.get("children", []))


def physical_roots(major_minor, visited=None):
    """Resolve mounted partitions/mappings back to whole media, using sysfs."""
    visited = set() if visited is None else visited
    if major_minor in visited:
        raise Refusal("Cycle in block device graph")
    visited.add(major_minor)
    link = Path("/sys/dev/block") / major_minor
    if not link.exists():
        raise Refusal("Cannot resolve block source " + major_minor)
    path = link.resolve()
    if (path / "partition").exists():
        return physical_roots((path.parent / "dev").read_text().strip(), visited)
    slaves = list((path / "slaves").glob("*"))
    if slaves:
        result = set()
        for slave in slaves:
            result |= physical_roots((slave / "dev").read_text().strip(), visited.copy())
        return result
    if path.name.startswith(("loop", "ram", "dm-", "md")):
        raise Refusal("Unsupported installation media/mapping source: " + path.name)
    return {path.name}


def media_disks():
    source = output("findmnt", "-n", "-o", "MAJ:MIN", "--mountpoint", str(MEDIA))
    if not re.fullmatch(r"[0-9]+:[0-9]+", source):
        raise Refusal("Installation media is not an identifiable block mount")
    roots = physical_roots(source)
    if not roots or not (MEDIA / "UBUNTU_CUSTOMIZER_MEDIA").is_file():
        raise Refusal("Cannot identify installation media")
    return roots


def disk_identity(node):
    name = Path(node["path"]).name
    path = Path("/sys/class/block") / name
    if any(part.startswith("usb") for part in path.resolve().parts):
        raise Refusal("USB-attached disks are excluded")
    sequence = (path / "diskseq").read_text().strip() if (path / "diskseq").exists() else ""
    identity = (node.get("wwn") or "", node.get("serial") or "", sequence, str(path.resolve()))
    if not any(identity[:3]):
        raise Refusal("No stable identity or kernel disk sequence for " + node["path"])
    return (node["maj:min"], int(node["size"]), int(node["log-sec"]), *identity)


def eligible_disks(nodes, excluded, occupied, raw_bytes, identity):
    result = []
    for node in nodes:
        name = Path(node["path"]).name
        if node.get("type") != "disk" or name in excluded or name in occupied:
            continue
        if node.get("ro") not in (False, 0) or node.get("rm") not in (False, 0) or node.get("tran") == "usb":
            continue
        if int(node.get("log-sec", 0)) != 512 or int(node["size"]) < raw_bytes + 1024 ** 2:
            continue
        if any(child.get("mountpoints") and any(child["mountpoints"]) for child in flatten([node])):
            continue
        try:
            node = {**node, "identity": identity(node)}
        except (OSError, Refusal):
            continue
        result.append(node)
    # Deterministic equal-size choice; identity includes kernel disk sequence as a final tie breaker.
    return sorted(result, key=lambda disk: (-int(disk["size"]), str(disk.get("wwn") or ""), str(disk.get("serial") or ""), disk["identity"], disk["path"]))


def discover(raw_bytes):
    excluded = media_disks()  # Fail closed BEFORE considering any candidate.
    tree = json.loads(output("lsblk", "--json", "--bytes", "--output", "NAME,PATH,TYPE,SIZE,RO,RM,LOG-SEC,TRAN,MODEL,SERIAL,WWN,MAJ:MIN,MOUNTPOINTS"))["blockdevices"]
    occupied = set()
    # Active mappings/arrays and mounts on every descendant exclude the entire parent disk.
    for node in flatten(tree):
        sys_path = Path("/sys/class/block") / Path(node["path"]).name
        if list((sys_path / "holders").glob("*")) or any(node.get("mountpoints") or []):
            occupied |= physical_roots(node["maj:min"])
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        source = line.split()[0]
        info = os.stat(source)
        number = info.st_rdev if stat.S_ISBLK(info.st_mode) else info.st_dev
        occupied |= physical_roots(f"{os.major(number)}:{os.minor(number)}")
    return eligible_disks(tree, excluded, occupied, raw_bytes, disk_identity)


def highlight(text, *, color="33"):
    # Keep redirected logs and basic terminals readable without escape codes.
    if (sys.stdout.isatty() or (SESSION is not None and SESSION.console_isatty)) and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ:
        return f"\033[1;{color}m{text}\033[0m"
    return text


def interaction_panel(title, lines, *, color="33"):
    border = "=" * 70
    print("\n\n" + highlight(border, color=color))
    print(highlight(title, color=color))
    print(highlight(border, color=color))
    for line in lines:
        print(line)
    print(highlight(border, color=color), flush=True)


def welcome(prompt=None):
    interaction_panel("LINUX IMAGE BUILDER", [
        "       L I N U X   I M A G E   B U I L D E R",
        "",
        "  Customized Ubuntu / Debian Server | x86_64",
        "  Alpine Live Installer | Offline whole-disk installation",
        "",
        "[ACTION REQUIRED 1/3] START INSTALLER",
        "  The installer is paused. No target disk has been written.",
        "  Continue to verify the image and select an installation disk.",
        "  Installing will replace ALL data on the selected disk.",
        "  A separate disk erasure confirmation is required before writing.",
        "",
        "Type y + Enter to continue. Any other answer cancels.",
        "No timeout. The disk selection timer has NOT started.",
    ], color="36")
    if not sys.stdin.isatty():
        raise Refusal("Interactive local terminal required")
    if prompt is None:
        prompt = input
    if prompt(highlight(">>> START INSTALLER [y = continue / other = cancel]: ", color="36")).lower() != "y":
        raise Refusal("Installer start not confirmed")


def select_disk(disks, timeout=30):
    if not disks:
        raise Refusal("No eligible disk. USB, removable, busy, read-only and incompatible disks are excluded.")
    interaction_panel("[ACTION REQUIRED 2/3] SELECT INSTALLATION DISK", [
        "No target disk has been written.",
        f"After {timeout}s with no input, the largest eligible disk is selected.",
        "Selection NEVER authorizes erasure; a separate confirmation follows.",
    ])
    for index, disk in enumerate(disks, 1):
        print(f"  {index}. {disk['path']}  {disk.get('model') or ''}  {int(disk['size']) / 1024**3:.1f} GiB  serial={disk.get('serial') or '-'}" + ("  [DEFAULT / LARGEST]" if index == 1 else ""))
    print(highlight("Enter disk number + Enter | Enter = default | q = cancel"))
    print("Any key stops the automatic selection timer.\n", flush=True)
    import termios
    import tty
    if not sys.stdin.isatty():
        raise Refusal("Interactive local terminal required")
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    deadline = time.monotonic() + timeout
    value = ""
    active = True
    try:
        tty.setcbreak(fd)
        while True:
            remaining = max(0, deadline - time.monotonic()) if active else None
            if active:
                print("\r" + highlight(f">>> SELECT DISK [{remaining:2.0f}s to default]: {value}".ljust(70)), end="", flush=True)
            ready, _, _ = select.select([fd], [], [], min(1, remaining) if active else None)
            if not ready:
                if active and time.monotonic() >= deadline:
                    print("\nLargest eligible disk selected; waiting for destructive confirmation.")
                    return disks[0]
                continue
            char = os.read(fd, 1).decode("ascii", errors="ignore")
            if not char or char in ("q", "Q", "\x03", "\x1b", "\x04"):
                raise Refusal("Cancelled")
            active = False
            if char in ("\r", "\n"):
                print()
                if value == "":
                    return disks[0]
                if value.isdigit() and 1 <= int(value) <= len(disks):
                    return disks[int(value) - 1]
                print(highlight("INVALID SELECTION: enter a disk number; q cancels."))
                value = ""
            elif char in ("\x7f", "\b"):
                value = value[:-1]
            elif char.isprintable() and len(value) < 20:
                value += char
            print("\r" + highlight(f">>> SELECT DISK [timer stopped]: {value}".ljust(70)), end="", flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def confirm(disk, prompt=input):
    interaction_panel("[ACTION REQUIRED 3/3] CONFIRM DISK ERASURE", [
        highlight("WARNING: ALL DATA ON THIS DISK WILL BE LOST.", color="31"),
        "",
        f"  Target : {disk['path']}",
        f"  Model  : {disk.get('model') or '-'}",
        f"  Serial : {disk.get('serial') or '-'}",
        f"  Size   : {int(disk['size']) / 1024**3:.1f} GiB ({disk['size']} bytes)",
        "",
        "WAITING FOR YOUR INPUT - no timeout, no automatic erasure.",
        "Type y + Enter to erase this disk and install.",
        "Enter alone or any other answer cancels installation.",
    ], color="31")
    if prompt(highlight(">>> CONFIRM ERASURE [y = erase / other = cancel]: ", color="31")).lower() != "y":
        raise Refusal("Erasure not confirmed")


def revalidate(selected, raw_bytes):
    matches = [disk for disk in discover(raw_bytes) if disk["path"] == selected["path"] and disk["identity"] == selected["identity"]]
    if len(matches) != 1:
        raise Refusal("Disk disappeared, changed identity, or became busy after selection")


def wipe_old_signatures(fd, selected, raw_bytes):
    """After confirmation only: erase old signatures BEFORE writing the image.

    Image writes cover only the template length. Old ZFS/RAID signatures near
    the end of a larger partition would otherwise survive and confuse Ubuntu's
    UUID discovery. Wipe old partitions before erasing the partition table.
    """
    revalidate(selected, raw_bytes)
    # Explicit --tree is required: lsblk otherwise flattens JSON when NAME is
    # omitted, so PATH-only output would hide the parent/child relationship.
    tree = json.loads(output("lsblk", "--json", "--tree", "--paths", "--output", "PATH,TYPE,MAJ:MIN", selected["path"]))["blockdevices"]
    if len(tree) != 1 or tree[0].get("path") != selected["path"] or tree[0].get("type") != "disk" or tree[0].get("maj:min") != selected["maj:min"]:
        raise Refusal("Cannot identify confirmed disk for signature cleanup")
    partitions = tree[0].get("children", [])
    for node in partitions:
        if node.get("type") != "part" or node.get("children") or not re.fullmatch(r"/dev/[A-Za-z0-9/_-]+", node.get("path", "")):
            raise Refusal("Unexpected child device during signature cleanup")
        if physical_roots(node["maj:min"]) != {Path(selected["path"]).name}:
            raise Refusal("Partition is outside the confirmed disk")

    def erase(opened, major_minor):
        info = os.fstat(opened)
        if not stat.S_ISBLK(info.st_mode) or f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}" != major_minor:
            raise Refusal("Signature cleanup device identity changed")
        revalidate(selected, raw_bytes)
        # Use the verified, already opened device, not a path that could be
        # replaced between checking it and starting wipefs. No global /dev glob.
        sp.run(["wipefs", "--all", "--force", "--", f"/proc/self/fd/{opened}"],
               pass_fds=(opened,), check=True)

    print("Clearing old filesystem/RAID signatures on the confirmed disk before image writing...")
    for node in partitions:
        partition_fd = os.open(node["path"], os.O_RDWR | os.O_CLOEXEC)
        try:
            if physical_roots(node["maj:min"]) != {Path(selected["path"]).name}:
                raise Refusal("Partition parent changed during signature cleanup")
            erase(partition_fd, node["maj:min"])
        finally:
            os.close(partition_fd)
    erase(fd, selected["maj:min"])
    os.fsync(fd)
    revalidate(selected, raw_bytes)
    os.lseek(fd, 0, os.SEEK_SET)


def clear_unwritten_tail(fd, selected, raw_bytes):
    """Clear end-of-disk labels outside the verified image before root expansion."""
    revalidate(selected, raw_bytes)
    info = os.fstat(fd)
    if not stat.S_ISBLK(info.st_mode) or f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}" != selected["maj:min"]:
        raise Refusal("Tail cleanup device identity changed")
    size = os.lseek(fd, 0, os.SEEK_END)
    if size != int(selected["size"]) or not 0 < raw_bytes <= size:
        raise Refusal("Tail cleanup disk or image size changed")
    start = max(raw_bytes, size - 4 * 1024 ** 2)
    remaining = size - start
    print(f"Clearing {remaining} bytes of old disk-tail labels at offset {start}, outside the verified image...")
    os.lseek(fd, start, os.SEEK_SET)
    zeros = bytes(1024 ** 2)
    while remaining:
        count = os.write(fd, zeros[:min(remaining, len(zeros))])
        if count <= 0:
            raise OSError("Short disk-tail write")
        remaining -= count
    os.fsync(fd)
    import fcntl
    fcntl.ioctl(fd, 0x1261)
    os.lseek(fd, start, os.SEEK_SET)
    remaining = size - start
    while remaining:
        chunk = os.read(fd, min(remaining, len(zeros)))
        if not chunk or any(chunk):
            raise Refusal("Disk-tail cleanup readback failed")
        remaining -= len(chunk)
    revalidate(selected, raw_bytes)


def write_image(fd, image, expected_bytes, expected_hash):
    written = 0
    hasher = hashlib.sha256()
    decompressor = sp.Popen(["zstd", "--decompress", "--stdout", "--", str(image)], stdout=sp.PIPE)
    try:
        while True:
            chunk = decompressor.stdout.read(8 * 1024 ** 2)
            if not chunk:
                break
            if written + len(chunk) > expected_bytes:
                raise Refusal("Image exceeds declared size")
            hasher.update(chunk)
            view = memoryview(chunk)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("Short disk write")
                view = view[count:]
            written += len(chunk)
            print(f"\rWriting {written / expected_bytes:.1%}", end="", flush=True)
        if decompressor.wait() != 0 or written != expected_bytes or hasher.hexdigest() != expected_hash:
            raise Refusal("Decompression, image length or raw hash failed")
        os.fsync(fd)
        if stat.S_ISBLK(os.fstat(fd).st_mode):
            import fcntl
            fcntl.ioctl(fd, 0x1261)  # BLKFLSBUF: flush/invalidate block cache before readback.
    finally:
        decompressor.stdout.close()
        if decompressor.poll() is None:
            decompressor.terminate()
            decompressor.wait()
    installation_stage("Reading back and verifying written image SHA256")
    print("\nReading back written image before partition/configuration changes...")
    os.lseek(fd, 0, os.SEEK_SET)
    remaining = expected_bytes
    hasher = hashlib.sha256()
    while remaining:
        chunk = os.read(fd, min(8 * 1024 ** 2, remaining))
        if not chunk:
            raise Refusal("Short disk readback")
        hasher.update(chunk)
        remaining -= len(chunk)
    if hasher.hexdigest() != expected_hash:
        raise Refusal("Disk readback SHA256 mismatch")


def main(*, check_tools_only=False):
    installation_stage("Checking Live installer prerequisites")
    if os.geteuid() != 0:
        raise Refusal("Root required inside Alpine Live")
    check_live_tools()
    if check_tools_only:
        print("Alpine Live installer tools checked; no disks accessed.")
        return
    installation_stage("Waiting for installer start confirmation")
    welcome()
    installation_stage("Validating installation media and manifest")
    manifest = json.loads((PAYLOAD / "manifest.json").read_text())
    validate_manifest(manifest)
    installation_stage("Verifying compressed system image", build_id=manifest["build_id"])
    if (MEDIA / "UBUNTU_CUSTOMIZER_MEDIA").read_text().strip() != manifest["build_id"]:
        raise Refusal("Media and payload build IDs differ")
    print("Verifying compressed payload; no target writes yet...")
    image = PAYLOAD / ('system.img.zst' if manifest['format'] == 2 else 'ubuntu.img.zst')
    if sha256_file(image) != manifest["compressed_sha256"]:
        raise Refusal("Compressed payload SHA256 mismatch")
    sp.run(["zstd", "--test", "--", str(image)], check=True)
    installation_stage("Discovering and selecting target disk")
    selected = select_disk(discover(manifest["raw_bytes"]))
    installation_stage("Waiting for disk erasure confirmation", disk=selected["path"])
    confirm(selected)  # The ONLY destructive confirmation. Nothing above writes a target.
    installation_stage("Revalidating and opening confirmed target disk")
    revalidate(selected, manifest["raw_bytes"])
    fd = os.open(selected["path"], os.O_RDWR | os.O_EXCL | os.O_CLOEXEC)
    try:
        device = os.fstat(fd)
        if not stat.S_ISBLK(device.st_mode) or f"{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}" != selected["maj:min"]:
            raise Refusal("Opened device is not the confirmed disk")
        revalidate(selected, manifest["raw_bytes"])
        installation_stage("Clearing old filesystem and RAID signatures", writes_started=True)
        wipe_old_signatures(fd, selected, manifest["raw_bytes"])
        installation_stage("Writing and verifying system image")
        write_image(fd, image, manifest["raw_bytes"], manifest["raw_sha256"])
        installation_stage("Clearing old labels beyond the verified image before root expansion")
        clear_unwritten_tail(fd, selected, manifest["raw_bytes"])
    finally:
        os.close(fd)
    # The raw image now contains its own partitions, but the whole-device identity must persist.
    if disk_identity(selected) != selected["identity"]:
        raise Refusal("Target identity changed during write")
    installation_stage("Finalizing installed system")
    finalize_env = {**os.environ, "UBUNTU_CUSTOMIZER_WRITE_VERIFIED": "1"}
    if SESSION is not None:
        finalize_env.update(UBUNTU_CUSTOMIZER_STAGE_FILE=str(SESSION.stage_path),
                            UBUNTU_CUSTOMIZER_ERROR_FILE=str(SESSION.command_path))
    target_user = manifest['username'] if manifest['format'] == 2 else 'ubuntu'
    target_distro = manifest['distro'] if manifest['format'] == 2 else 'ubuntu'
    target_version = manifest['version'] if manifest['format'] == 2 else '24.04'
    sp.run(["bash", "/usr/local/lib/ubuntu-customizer/finalize.sh", selected["path"], manifest["build_id"],
            target_user, target_distro, target_version],
           env=finalize_env, check=True)
    interaction_panel("[SUCCESS] INSTALLATION COMPLETE", [
        f"  Installed disk : {selected['path']}",
        f"  Build ID       : {manifest['build_id']}",
        "",
        "NEXT STEPS",
        "  1. Remove the installation USB (or disconnect the ISO in your VM).",
        "  2. Reboot manually from the Live shell below:",
        "       reboot",
        f"  3. Log in to the installed {target_distro} system:",
        f"       Username: {target_user} | Password: your preset image password",
        "  4. Change your login password:",
        "       passwd",
        "  5. If needed, authenticate Tailscale and follow its login link:",
        "       sudo tailscale up",
        "  6. For Docker image downloads, open http://<server-ip>:2017",
        "       Create a v2rayA account, import a node and connect/start it.",
        "       Docker is already configured to use the local proxy.",
        "",
        "Run passwd and tailscale up AFTER booting into the installed system.",
        "The machine will NOT reboot automatically.",
    ], color="32")


def run_installer():
    global SESSION
    try:
        SESSION = InstallationReport()
        sys.stdout.reconfigure(line_buffering=True)
        error = None
        with SESSION.capture():
            print(f"Installation log: {SESSION.log_path}", flush=True)
            try:
                main()
            except (Exception, KeyboardInterrupt) as caught:
                error = caught
        if SESSION.capture_error is not None:
            print(f"Installation log write failed: {SESSION.capture_error}", file=sys.stderr)
        if error is not None:
            with SESSION.log_path.open("a", encoding="utf-8") as log:
                traceback.print_exception(type(error), error, error.__traceback__, file=log)
            SESSION.failure(error)
            return 1
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f"INSTALLATION STOPPED: {type(error).__name__}: {error}", file=sys.stderr)
        print("Error reporting could not complete. Installation is not confirmed; do not reboot until disks are unmounted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-tools", action="store_true", help="Check Live tool availability only; do not access disks")
    args = parser.parse_args()
    if args.check_tools:
        main(check_tools_only=True)
    else:
        sys.exit(run_installer())

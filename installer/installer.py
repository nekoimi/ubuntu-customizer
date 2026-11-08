#!/usr/bin/env python3
"""Offline whole-disk installer. Discovery and confirmation precede every write."""
import argparse
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
import time

MEDIA = Path("/media/installer")
PAYLOAD = MEDIA / "payload"
# Includes tools used by finalize.sh outside the Ubuntu chroot, where missing
# Alpine subpackages would otherwise be discovered only AFTER overwriting a disk.
LIVE_TOOLS = ("bash", "zstd", "lsblk", "findmnt", "sgdisk", "parted", "partprobe",
              "e2fsck", "resize2fs", "tune2fs", "fatlabel", "blkid", "mount", "umount",
              "chroot", "awk", "od", "tr", "mkdir", "install", "dd", "chmod", "date", "sync")


class Refusal(RuntimeError):
    pass


def check_live_tools():
    missing = [tool for tool in LIVE_TOOLS if not shutil.which(tool)]
    if missing:
        raise Refusal("Missing Alpine Live tools: " + ", ".join(missing) + "; rebuild the installer ISO. No target disk has been written.")


def output(*args):
    return sp.check_output(args, text=True).strip()


def sha256_file(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 ** 2), b""):
            value.update(chunk)
    return value.hexdigest()


def validate_manifest(manifest):
    if manifest.get("format") != 1 or manifest.get("architecture") != "amd64":
        raise Refusal("Unsupported payload format or architecture")
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
    if sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ:
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
    interaction_panel("UBUNTU CUSTOMIZER", [
        "       _   _ ____  _   _ _   _ _____ _   _",
        "      | | | | __ )| | | | \\ | |_   _| | | |",
        "      | | | |  _ \\| | | |  \\| | | | | | | |",
        "      | |_| | |_) | |_| | |\\  | | | | |_| |",
        "       \\___/|____/ \\___/|_| \\_| |_|  \\___/",
        "",
        "  Customized Ubuntu Server 24.04 LTS | x86_64",
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
    if os.geteuid() != 0:
        raise Refusal("Root required inside Alpine Live")
    check_live_tools()
    if check_tools_only:
        print("Alpine Live installer tools checked; no disks accessed.")
        return
    welcome()
    manifest = json.loads((PAYLOAD / "manifest.json").read_text())
    validate_manifest(manifest)
    if (MEDIA / "UBUNTU_CUSTOMIZER_MEDIA").read_text().strip() != manifest["build_id"]:
        raise Refusal("Media and payload build IDs differ")
    print("Verifying compressed payload; no target writes yet...")
    image = PAYLOAD / "ubuntu.img.zst"
    if sha256_file(image) != manifest["compressed_sha256"]:
        raise Refusal("Compressed payload SHA256 mismatch")
    sp.run(["zstd", "--test", "--", str(image)], check=True)
    selected = select_disk(discover(manifest["raw_bytes"]))
    confirm(selected)  # The ONLY destructive confirmation. Nothing above writes a target.
    revalidate(selected, manifest["raw_bytes"])
    fd = os.open(selected["path"], os.O_RDWR | os.O_EXCL | os.O_CLOEXEC)
    try:
        device = os.fstat(fd)
        if not stat.S_ISBLK(device.st_mode) or f"{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}" != selected["maj:min"]:
            raise Refusal("Opened device is not the confirmed disk")
        revalidate(selected, manifest["raw_bytes"])
        write_image(fd, image, manifest["raw_bytes"], manifest["raw_sha256"])
    finally:
        os.close(fd)
    # The raw image now contains its own partitions, but the whole-device identity must persist.
    if disk_identity(selected) != selected["identity"]:
        raise Refusal("Target identity changed during write")
    sp.run(["bash", "/usr/local/lib/ubuntu-customizer/finalize.sh", selected["path"], manifest["build_id"]],
           env={**os.environ, "UBUNTU_CUSTOMIZER_WRITE_VERIFIED": "1"}, check=True)
    interaction_panel("[SUCCESS] INSTALLATION COMPLETE", [
        f"  Installed disk : {selected['path']}",
        f"  Build ID       : {manifest['build_id']}",
        "",
        "NEXT STEPS",
        "  1. Remove the installation USB (or disconnect the ISO in your VM).",
        "  2. Reboot manually from the Live shell below:",
        "       reboot",
        "  3. Log in to the installed Ubuntu system:",
        "       Username: ubuntu | Password: your preset image password",
        "  4. Change your login password:",
        "       passwd",
        "  5. If needed, authenticate Tailscale and follow its login link:",
        "       sudo tailscale up",
        "  6. For Docker image downloads, open http://<server-ip>:2017",
        "       Create a v2rayA account, import a node and connect/start it.",
        "       Docker is already configured to use the local proxy.",
        "",
        "Run passwd and tailscale up AFTER booting into installed Ubuntu.",
        "The machine will NOT reboot automatically.",
    ], color="32")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-tools", action="store_true", help="Check Live tool availability only; do not access disks")
    args = parser.parse_args()
    try:
        main(check_tools_only=args.check_tools)
    except (Exception, KeyboardInterrupt) as error:
        print("\nINSTALLATION STOPPED: " + str(error), file=sys.stderr)
        print("No success is claimed. If writing started, the selected disk may be incomplete.", file=sys.stderr)
        sys.exit(1)

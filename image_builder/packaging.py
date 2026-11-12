"""Shared Alpine Live environment and hybrid ISO packaging."""
import gzip
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import subprocess as sp
import time
from image_builder.core import REPO, alpine_mirrors, digest, download


class AlpineISO:
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
        (library / 'bootloader.sh').write_text(self.target.guest_script('bootloader'))
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
        self.note("Hashing and compressing the complete target disk image")
        with self.progress("Calculating target disk image SHA256"):
            raw_hash = digest(raw)
        self.details.update({"format": 2, "payload": "system.img.zst", "root_filesystem": "ext4", "finalizer": "debian-family-v1", "sector_size": 512, "root_partition": 3, "esp_partition": 2,
                             "raw_bytes": raw.stat().st_size, "raw_sha256": raw_hash})
        compressed = payload / "system.img.zst"
        self.run(["zstd", "-T" + str(self.config["compression_threads"]), "-8", raw, "-o", compressed], label="Compressing target disk image (zstd)")
        self.run(["zstd", "--test", compressed], label="Verifying compressed disk image")
        self.checks["compressed_stream"] = "passed"
        with self.progress("Calculating compressed image SHA256"):
            self.details["compressed_sha256"] = digest(compressed)
        self.alpine(iso)
        self.details["sources"] = {key: self.config[key] for key in ("system_mirror", "security_mirror", "docker_mirror", "python_index")}
        self.details["sources"]["tailscale"] = "https://pkgs.tailscale.com/stable/" + self.target.name
        self.details["git_commit"] = None
        if shutil.which("git"):
            git = sp.run(["git", "-c", "safe.directory=" + str(REPO), "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True)
            if git.returncode == 0:
                self.details["git_commit"] = git.stdout.strip()
        self.details["input_sha256"] = {str(path.relative_to(REPO)): digest(path)
                                         for directory in ("scripts", "installer", "config", "image_builder", "distros", "components", "profiles")
                                         for path in sorted((REPO / directory).rglob("*"))
                                         if path.is_file() and path.suffix in (".py", ".sh", ".bash", ".json", ".txt", ".gpg")}
        self.details["input_sha256"]["build.sh"] = digest(REPO / "build.sh")
        manifest = json.dumps(self.details, indent=2) + "\n"
        (payload / "manifest.json").write_text(manifest)
        (payload / "SHA256SUMS").write_text(self.details["compressed_sha256"] + "  system.img.zst\n")
        (iso / "UBUNTU_CUSTOMIZER_MEDIA").write_text(self.id + "\n")
        (iso / "boot/grub").mkdir(parents=True)
        (iso / "boot/grub/grub.cfg").write_text(f'set timeout=3\nset default=0\nmenuentry "Install customized {self.target.name} {self.target.version} Server (erases selected disk after confirmation)" {{\n linux /boot/vmlinuz console=tty0\n initrd /boot/initramfs\n}}\n')
        shutil.copyfile(REPO / "LICENSE", iso / "LICENSE")
        # Package copyright notices accompany the installed system; Alpine notices remain in live root.
        output = self.release / (self.target.iso_name + ".partial")
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
        output.replace(self.release / self.target.iso_name)
        (self.release / "manifest.json").write_text(manifest)
        (self.release / "SHA256SUMS").write_text(iso_hash + "  " + self.target.iso_name + "\n")
        os.sync()
        self.note("Hybrid ISO created; package/configuration and image integrity checks passed")

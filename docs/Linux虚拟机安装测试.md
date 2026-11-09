# Linux 服务器安装测试

使用 QEMU/KVM 在构建服务器上直接测试现有 ISO，无需制作 U 盘。默认采用 UEFI、虚拟 NVMe、4 vCPU 和 4 GiB 内存。测试盘是独立目录内的 qcow2 文件，启动器不会传入宿主物理磁盘、修改宿主分区或配置桥接网络。

虚拟机用于检查安装脚本、引导、旧磁盘状态和异常处理。它不保证复现某块实体 NVMe 控制器的驱动或事件时序问题；最近这次 Ubuntu `blkid` 探测失败的具体原因也尚未确认。即使虚拟机安装通过，仍需保留实机验收。

## 准备和启动

在 Linux x86_64 服务器安装可选测试依赖：

```bash
sudo apt-get update
sudo apt-get install -y qemu-system-x86 qemu-utils ovmf
ls -l /dev/kvm
```

运行测试的用户需要有 `/dev/kvm` 读写权限；如果没有，可由管理员将其加入 `kvm` 组后重新登录。测试入口不自动提权。虚拟磁盘按实际写入增长，建议至少预留 40 GiB 可用空间，后续根据磁盘占用调整。

在项目目录启动，将 ISO 路径换成实际产物：

```bash
bash build.sh vm-test \
  --iso /srv/ubuntu-customizer/releases/<build-id>/ubuntu-custom-server.iso \
  --state-dir "$HOME/ubuntu-vm-tests/uefi-nvme" \
  --disk-gib 500
```

500 GiB 是虚拟容量，便于覆盖“大磁盘安装小母镜像再扩容”的场景。默认容量是 80 GiB；`--disk-gib` 只在首次创建测试盘时生效。重复使用同一 `--state-dir` 会保留磁盘内容，安装器仍需正常选盘和确认清盘；新测试盘请使用新目录。

目录内保存 `disk.qcow2`、`OVMF_VARS.fd`、`vm.json`、`qemu.log` 和运行锁。UEFI 变量为该虚拟机私有副本，固件代码只读。已有非测试文件的目录会被拒绝，不覆盖已有磁盘；带 backing file 或外部数据文件的 qcow2 也会被拒绝。

## 从 Windows 操作安装界面

在 Windows 终端建立 SSH 隧道，服务器地址替换成实际地址：

```powershell
ssh -N -L 5901:127.0.0.1:5901 -L 2222:127.0.0.1:2222 -L 18000:127.0.0.1:18000 nekoimi@10.1.1.100
```

保持隧道窗口打开，用 VNC 客户端连接本机 `127.0.0.1` 的 `5901` 端口，即可看到固件、GRUB 和完整安装界面。浏览器不能直接打开 VNC 地址。安装流程与实体机一致：欢迎确认、选盘、清盘确认、写入和收尾。通过 VNC 菜单发送 Ctrl+Alt+F2，可切换到 Live 第二终端。

默认只监听服务器回环地址，不需要开放服务器的 VNC 或 SSH 转发端口。端口冲突时可用 `--vnc-display 2 --ssh-port 2223 --log-port 18001`，并相应修改 SSH 隧道。VNC TCP 端口为 `5900 + display`。

## 保留失败现场和下载日志

出错后不要关闭 QEMU；Live 日志位于虚拟机内存。`qemu.log` 是模拟器日志，不能替代虚拟机内的 `installation.log`。

在 Live 第二终端执行：

```sh
ip -br link
udhcpc -i eth0
python3 -m http.server 8000 --bind 0.0.0.0 --directory /var/log/ubuntu-customizer
```

`eth0` 按 `ip -br link` 的实际网卡名替换。随后 Windows 浏览器打开 `http://127.0.0.1:18000/`，进入对应 `install-*` 目录下载完整日志。HTTP 服务在前台运行，日志取出后 Ctrl+C 停止即可。

QEMU 默认使用受限的用户态网络：DHCP 和上述端口转发可用，来宾不能访问外网。`--internet` 可显式启用外网访问；镜像若配置了 ntfy，联网启动后可能按预置策略发送通知。安装本身不需要外网。

## 安装后启动和重复测试

确认安装完成并取出需要的日志后，在启动 QEMU 的服务器终端按 Ctrl+C 停止虚拟机，再从已安装的虚拟磁盘启动：

```bash
bash build.sh vm-test \
  --state-dir "$HOME/ubuntu-vm-tests/uefi-nvme" \
  --boot installed
```

此模式不挂载 ISO，保留原来的固件模式和 UEFI 变量。Windows 可在上述隧道中通过 SSH 登录：

```powershell
ssh -p 2222 ubuntu@127.0.0.1
```

要测试 BIOS，请用新目录启动 `--firmware bios`。同一测试目录不能切换固件模式。模拟器退出码为 0 只说明 QEMU 正常退出，不能判定安装或引导通过。

VM 停止后，可以复制整个测试目录保留一个测试基线，再用副本重复测试。不要在 QEMU 运行期间直接复制活动 qcow2 作为一致性快照。

### 已确认问题的复现方式

| 问题 | 测试办法 |
|---|---|
| 大盘扩容、NVMe 命名 | 使用 500 GiB 虚拟 NVMe，执行完整安装，再用 `--boot installed` 验证启动 |
| 旧 ZFS `boot-pool` 残留 | 在专用虚拟盘先安装产生 ZFS 启动池的系统，停止并备份整个测试目录，再用定制 ISO 覆盖安装；空白 qcow2 不具备这种旧签名 |
| 内核有分区但 `/dev` 节点缺失 | 可在 Live 恢复终端使用下面的隔离节点测试；验证恢复逻辑，但不保证重现实体机造成节点丢失的时序 |
| Ubuntu / Alpine 探测结果不同 | 在同一次失败现场分别保留两套工具的 `blkid -p` 退出码、`wipefs --no-act` 输出及完整安装日志，不能仅根据“可能冲突”提示认定 ZFS 残留 |

隔离节点测试在临时目录创建块节点，不删除 Live 的真实 `/dev` 节点，也不打开或写入磁盘数据。请在已经产生分区的测试 VM 中运行：

```sh
python3 - <<'PY'
from pathlib import Path
import runpy
import tempfile

tools = runpy.run_path('/usr/local/lib/ubuntu-customizer/partition_devices.py')
with tempfile.TemporaryDirectory(prefix='partition-node-test-') as folder:
    nodes = Path(folder)
    disk = '/dev/nvme0n1'
    before = tools['probe'](disk, device_root=nodes)
    print('Before recovery:', before)
    assert before is None
    after = tools['probe'](disk, device_root=nodes, create_missing=True)
    print('After recovery:', after)
    assert after is not None
PY
```

新版本应先报告缺少节点，随后在临时目录按 sysfs 的实际设备号补建节点并返回根分区和 EFI 分区路径。这项测试验证节点恢复，不等同于完整安装验收。
